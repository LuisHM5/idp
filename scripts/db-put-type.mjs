#!/usr/bin/env node
/**
 * CLI: Insertar un tipo documental en DynamoDB para hot-reload sin redeploy.
 *
 * Uso:
 *   pnpm db-put-type --type ACTA_NACIMIENTO \
 *     --fields "NOMBRE_COMPLETO,CURP,FECHA_NACIMIENTO" \
 *     --required "NOMBRE_COMPLETO,CURP" \
 *     --rules '[{"field":"CURP","rule_type":"regex","pattern":"^[A-Z]{4}\\\\d{6}[HM][A-Z]{5}[A-Z0-9]\\\\d$"}]' \
 *     --hints '{"CURP":"CURP de 18 caracteres en la parte inferior del acta"}'
 *
 * Opciones:
 *   --type         (requerido) ID del tipo documental
 *   --table        Nombre de la tabla DynamoDB (default: env CATALOG_TABLE o "document-validation-catalog")
 *   --description  Descripción del tipo
 *   --fields       Campos a extraer (separados por coma)
 *   --required     Campos obligatorios (separados por coma)
 *   --rules        JSON array de reglas de validación
 *   --hints        JSON object de extraction_hints (campo → pista)
 *   --validator    Módulo Python de validación
 *   --custom       Script custom Node.js
 *   --disable      Marcar como deshabilitado (no se usará en validación)
 *   --region       AWS region (default: env AWS_REGION o "us-east-1")
 *   --help         Mostrar ayuda
 *
 * Requiere: AWS CLI configurado o credenciales en env.
 */

import { execSync } from "child_process";

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

function printHelp() {
  console.log(`
Uso: pnpm db-put-type --type TIPO --fields "CAMPO1,CAMPO2" [opciones]

Inserta o actualiza un tipo documental en DynamoDB.
Los cambios toman efecto inmediatamente sin necesidad de redeploy.

Opciones:
  --type         (requerido) ID del tipo documental
  --table        Tabla DynamoDB (default: CATALOG_TABLE env o "document-validation-catalog")
  --description  Descripción del tipo
  --fields       Campos a extraer (separados por coma)
  --required     Campos obligatorios (separados por coma)
  --rules        JSON array de reglas de validación
  --hints        JSON object de extraction_hints
  --validator    Módulo Python de validación
  --custom       Script custom Node.js (sin .mjs)
  --disable      Marcar como deshabilitado
  --region       AWS region (default: AWS_REGION env o "us-east-1")

Ejemplo:
  pnpm db-put-type --type ACTA_NACIMIENTO \\
    --description "Acta de nacimiento" \\
    --fields "NOMBRE_COMPLETO,CURP,FECHA_NACIMIENTO" \\
    --required "NOMBRE_COMPLETO,CURP" \\
    --hints '{"CURP":"CURP de 18 chars en la parte inferior"}'
`);
}

function toAttributeValue(val) {
  if (typeof val === "string") return { S: val };
  if (typeof val === "boolean") return { BOOL: val };
  if (typeof val === "number") return { N: String(val) };
  if (Array.isArray(val)) return { L: val.map(toAttributeValue) };
  if (val === null) return { NULL: true };
  if (typeof val === "object") {
    const m = {};
    for (const [k, v] of Object.entries(val)) {
      m[k] = toAttributeValue(v);
    }
    return { M: m };
  }
  return { S: String(val) };
}

function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args.help) { printHelp(); process.exit(0); }

  if (!args.type) {
    console.error("❌ --type es requerido\n");
    printHelp();
    process.exit(1);
  }

  const table = args.table || process.env.CATALOG_TABLE || "document-validation-catalog";
  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const typeId = args.type.toUpperCase().replace(/\s+/g, "_");

  const fields = args.fields ? args.fields.split(",").map(f => f.trim()) : [];
  const required = args.required ? args.required.split(",").map(f => f.trim()) : [];
  let rules = [];
  if (args.rules) {
    try { rules = JSON.parse(args.rules); } catch (e) {
      console.error(`❌ --rules JSON inválido: ${e.message}`);
      process.exit(1);
    }
  }
  let hints = {};
  if (args.hints) {
    try { hints = JSON.parse(args.hints); } catch (e) {
      console.error(`❌ --hints JSON inválido: ${e.message}`);
      process.exit(1);
    }
  }

  // Build DynamoDB item
  const item = {
    type_id: { S: typeId },
    description: { S: args.description || "" },
    extraction_fields: { L: fields.map(f => ({ S: f })) },
    required_fields: { L: required.map(f => ({ S: f })) },
    validation_rules: { L: rules.map(r => ({ M: {
      field: { S: r.field || "" },
      rule_type: { S: r.rule_type || "" },
      ...(r.pattern && { pattern: { S: r.pattern } }),
      ...(r.message && { message: { S: r.message } }),
    }}))},
    enabled: { BOOL: !args.disable },
  };

  if (Object.keys(hints).length > 0) {
    const hintsMap = {};
    for (const [k, v] of Object.entries(hints)) {
      hintsMap[k] = { S: v };
    }
    item.extraction_hints = { M: hintsMap };
  }

  if (args.validator) item.validator_module = { S: args.validator };
  if (args.custom) item.custom_validator = { S: args.custom };

  const itemJson = JSON.stringify(item);

  const cmd = `aws dynamodb put-item --table-name "${table}" --item '${itemJson}' --region ${region}`;

  console.log(`\n📤 Insertando tipo '${typeId}' en tabla '${table}' (${region})...\n`);

  try {
    execSync(cmd, { stdio: "inherit" });
    console.log(`\n✅ Tipo '${typeId}' insertado en DynamoDB.`);
    console.log(`   Efecto inmediato - no requiere redeploy.\n`);
  } catch (e) {
    console.error(`\n❌ Error insertando en DynamoDB.`);
    console.error(`   Verifica que la tabla '${table}' existe y tienes permisos.`);
    console.error(`   Comando ejecutado:\n   ${cmd}\n`);
    process.exit(1);
  }
}

main();
