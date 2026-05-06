#!/usr/bin/env node
/**
 * CLI: Agregar un nuevo tipo documental al catálogo JSON.
 *
 * Modo interactivo (sin flags):
 *   pnpm add-type
 *
 * Modo directo (con flags):
 *   pnpm add-type --type ACTA_NACIMIENTO --fields "NOMBRE,CURP" --required "NOMBRE"
 *
 * Opciones:
 *   --type         ID del tipo documental (MAYUSCULAS, guiones bajos)
 *   --description  Descripción legible del tipo
 *   --fields       Campos a extraer, separados por coma
 *   --required     Campos obligatorios, separados por coma
 *   --rules        JSON array de reglas de validación
 *   --hints        JSON object de extraction_hints (campo → pista)
 *   --validator    Módulo Python de validación
 *   --custom       Script custom Node.js (sin .mjs)
 *   --overwrite    Sobreescribir si ya existe
 *   --help         Mostrar ayuda
 */

import { readFileSync, writeFileSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import { createInterface } from "readline";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = resolve(
  __dirname,
  "../src/document-processing/resources/enrichment-lambda/config/document_types.json"
);

// --- Readline helpers ---

function createRL() {
  return createInterface({ input: process.stdin, output: process.stdout });
}

function ask(rl, question, defaultVal = "") {
  const suffix = defaultVal ? ` [${defaultVal}]` : "";
  return new Promise((resolve) => {
    rl.question(`${question}${suffix}: `, (answer) => {
      resolve(answer.trim() || defaultVal);
    });
  });
}

function askYesNo(rl, question, defaultVal = false) {
  const hint = defaultVal ? "[S/n]" : "[s/N]";
  return new Promise((resolve) => {
    rl.question(`${question} ${hint}: `, (answer) => {
      const a = answer.trim().toLowerCase();
      if (!a) return resolve(defaultVal);
      resolve(a === "s" || a === "si" || a === "sí" || a === "y" || a === "yes");
    });
  });
}

// --- Interactive mode ---

async function interactiveMode() {
  const rl = createRL();
  console.log("\n📄 Agregar nuevo tipo documental (modo interactivo)\n");

  const typeId = (await ask(rl, "ID del tipo (ej: ACTA_NACIMIENTO)")).toUpperCase().replace(/\s+/g, "_");
  if (!typeId) { console.error("❌ ID es requerido"); rl.close(); process.exit(1); }

  const description = await ask(rl, "Descripción");

  console.log("\n📋 Campos de extracción (lo que Bedrock debe buscar en el documento)");
  console.log("   Escribe los campos separados por coma.");
  console.log("   Ejemplo: NOMBRE_COMPLETO,CURP,RFC,FECHA_NACIMIENTO\n");
  const fieldsRaw = await ask(rl, "Campos");
  const fields = fieldsRaw ? fieldsRaw.split(",").map(f => f.trim().toUpperCase()) : [];

  let required = [];
  if (fields.length > 0) {
    console.log(`\n   Campos disponibles: ${fields.join(", ")}`);
    console.log("   ¿Cuáles son obligatorios? (separados por coma)\n");
    const reqRaw = await ask(rl, "Campos obligatorios");
    required = reqRaw ? reqRaw.split(",").map(f => f.trim().toUpperCase()) : [];
  }

  // Extraction hints
  const hints = {};
  if (fields.length > 0) {
    const wantHints = await askYesNo(rl, "\n¿Agregar pistas de extracción (extraction_hints)?", true);
    if (wantHints) {
      console.log("\n   Para cada campo, escribe una pista que le diga a la IA dónde encontrarlo.");
      console.log("   Deja vacío para saltar.\n");
      for (const field of fields) {
        const hint = await ask(rl, `   Pista para ${field}`);
        if (hint) hints[field] = hint;
      }
    }
  }

  // Validation rules
  const rules = [];
  const wantRules = await askYesNo(rl, "\n¿Agregar reglas de validación?", true);
  if (wantRules) {
    console.log("\n   Tipos de regla: regex, date_expiry, presence, boolean_true");
    console.log("   Escribe 'fin' para terminar.\n");

    let addingRules = true;
    while (addingRules) {
      const ruleField = await ask(rl, "   Campo a validar (o 'fin')");
      if (ruleField.toLowerCase() === "fin" || !ruleField) { addingRules = false; break; }

      const ruleType = await ask(rl, "   Tipo de regla (regex/date_expiry/presence/boolean_true)", "regex");

      let pattern = null;
      if (ruleType === "regex") {
        pattern = await ask(rl, "   Patrón regex");
        if (!pattern) { console.log("   ⚠️  regex requiere patrón, saltando..."); continue; }
      }

      const message = await ask(rl, "   Mensaje de error");

      const rule = { field: ruleField.toUpperCase(), rule_type: ruleType };
      if (pattern) rule.pattern = pattern;
      if (message) rule.message = message;
      rules.push(rule);

      console.log(`   ✓ Regla agregada: ${ruleField} (${ruleType})\n`);
    }
  }

  // Validator module
  const validator = await ask(rl, "\nMódulo Python validator (ej: validators.identification, vacío para ninguno)");

  // Custom validator
  const custom = await ask(rl, "Custom validator Node.js (nombre sin .mjs, vacío para ninguno)");

  rl.close();

  // Build entry
  const entry = {
    description,
    extraction_fields: fields,
    required_fields: required,
    validation_rules: rules,
    validator_module: validator || null,
  };
  if (Object.keys(hints).length > 0) entry.extraction_hints = hints;
  if (custom) entry.custom_validator = custom;

  // Save
  const config = JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
  if (config.types[typeId]) {
    console.log(`\n⚠️  El tipo '${typeId}' ya existe y será sobreescrito.`);
  }
  config.types[typeId] = entry;
  writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2) + "\n", "utf8");

  console.log(`\n✅ Tipo '${typeId}' agregado al catálogo.\n`);
  printSummary(typeId, entry);
  console.log(`📦 Para aplicar: pnpm build && pnpm deploy\n`);
}

// --- Flag mode ---

function parseArgs(args) {
  const parsed = {};
  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith("--")) {
      const key = args[i].slice(2);
      if (i + 1 >= args.length || args[i + 1].startsWith("--")) {
        parsed[key] = true;
      } else {
        parsed[key] = args[i + 1];
        i++;
      }
    }
  }
  return parsed;
}

function flagMode(args) {
  const typeId = args.type.toUpperCase().replace(/\s+/g, "_");
  const description = args.description || "";
  const fields = args.fields ? args.fields.split(",").map(f => f.trim()) : [];
  const required = args.required ? args.required.split(",").map(f => f.trim()) : [];

  let rules = [];
  if (args.rules) {
    try { rules = JSON.parse(args.rules); }
    catch (e) { console.error(`❌ --rules JSON inválido: ${e.message}`); process.exit(1); }
  }

  let hints = {};
  if (args.hints) {
    try { hints = JSON.parse(args.hints); }
    catch (e) { console.error(`❌ --hints JSON inválido: ${e.message}`); process.exit(1); }
  }

  const config = JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
  if (config.types[typeId] && !args.overwrite) {
    console.error(`❌ El tipo '${typeId}' ya existe. Usa --overwrite para sobreescribirlo.`);
    process.exit(1);
  }

  const entry = {
    description,
    extraction_fields: fields,
    required_fields: required,
    validation_rules: rules,
    validator_module: args.validator || null,
  };
  if (Object.keys(hints).length > 0) entry.extraction_hints = hints;
  if (args.custom) entry.custom_validator = args.custom;

  config.types[typeId] = entry;
  writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2) + "\n", "utf8");

  console.log(`\n✅ Tipo '${typeId}' agregado al catálogo.\n`);
  printSummary(typeId, entry);
  console.log(`📦 Para aplicar: pnpm build && pnpm deploy\n`);
}

function printSummary(typeId, entry) {
  console.log(`   ID:          ${typeId}`);
  console.log(`   Descripción: ${entry.description || "(sin descripción)"}`);
  console.log(`   Campos:      ${(entry.extraction_fields || []).join(", ") || "(ninguno)"}`);
  console.log(`   Requeridos:  ${(entry.required_fields || []).join(", ") || "(ninguno)"}`);
  console.log(`   Reglas:      ${(entry.validation_rules || []).length} regla(s)`);
  console.log(`   Hints:       ${Object.keys(entry.extraction_hints || {}).length} pista(s)`);
  if (entry.validator_module) console.log(`   Validator:   ${entry.validator_module}`);
  if (entry.custom_validator) console.log(`   Custom:      ${entry.custom_validator}.mjs`);
}

function printHelp() {
  console.log(`
Uso: pnpm add-type [opciones]

Sin opciones: modo interactivo (pregunta paso a paso)
Con opciones: modo directo

Opciones:
  --type         ID del tipo documental (requerido en modo directo)
  --description  Descripción del tipo
  --fields       Campos a extraer (separados por coma)
  --required     Campos obligatorios (separados por coma)
  --rules        JSON array de reglas de validación
  --hints        JSON object de extraction_hints (campo → pista)
  --validator    Módulo Python de validación
  --custom       Script custom Node.js (sin .mjs)
  --overwrite    Sobreescribir si ya existe
  --help         Mostrar esta ayuda

Ejemplos:
  # Interactivo
  pnpm add-type

  # Directo con regex
  pnpm add-type --type LICENCIA --fields "NOMBRE,NUMERO,VIGENCIA" \\
    --required "NOMBRE,NUMERO" \\
    --rules '[{"field":"VIGENCIA","rule_type":"date_expiry","message":"Licencia vencida"}]'

  # Con hints
  pnpm add-type --type RECIBO_LUZ --fields "NOMBRE,DIRECCION,PERIODO" \\
    --required "NOMBRE,DIRECCION" \\
    --hints '{"NOMBRE":"Nombre del titular en la parte superior","DIRECCION":"Dirección de suministro"}'
`);
}

// --- Main ---

async function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args.help) { printHelp(); process.exit(0); }

  // If --type is provided, use flag mode. Otherwise, interactive.
  if (args.type) {
    flagMode(args);
  } else if (Object.keys(args).length === 0) {
    await interactiveMode();
  } else {
    console.error("❌ En modo directo, --type es requerido.\n");
    printHelp();
    process.exit(1);
  }
}

main();
