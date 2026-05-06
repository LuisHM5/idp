#!/usr/bin/env node
/**
 * CLI: Crear un nuevo custom validator en Node.js.
 *
 * Uso:
 *   node scripts/add-custom-validator.mjs --name validate_rfc_sat
 *   node scripts/add-custom-validator.mjs --name validate_domicilio --type COMPROBANTE_DOMICILIO
 *
 * Opciones:
 *   --name   (requerido) Nombre del validator (sin .mjs)
 *   --type   (opcional) Tipo documental al que asociar automáticamente
 *   --help   Mostrar ayuda
 *
 * El script genera un archivo .mjs con la estructura correcta.
 * Si se pasa --type, también actualiza el JSON config para asociar
 * el custom validator al tipo indicado.
 */

import { writeFileSync, readFileSync, existsSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const VALIDATORS_DIR = resolve(
  __dirname,
  "../src/document-processing/resources/enrichment-lambda/custom_validators"
);
const CONFIG_PATH = resolve(
  __dirname,
  "../src/document-processing/resources/enrichment-lambda/config/document_types.json"
);

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
Uso: pnpm add-validator --name mi_validador [opciones]

Opciones:
  --name   (requerido) Nombre del validator (sin extensión .mjs)
  --type   Asociar automáticamente a un tipo documental del catálogo
  --help   Mostrar esta ayuda

Ejemplos:
  # Crear validator standalone
  pnpm add-validator --name validate_rfc_sat

  # Crear y asociar a un tipo
  pnpm add-validator --name validate_domicilio --type COMPROBANTE_DOMICILIO

Después de crear el validator:
  1. Edita la función validate() en custom_validators/<nombre>.mjs
  2. Si no usaste --type, asocia manualmente en config/document_types.json:
     "custom_validator": "<nombre>"
  3. Redespliega: pnpm build && npx cdk deploy

Interfaz del validator:
  export async function validate(entityMap, context) {
    // entityMap: { "RFC": "XAXX010101000", "CURP": "...", ... }
    // context: { documentId: "uuid", classification: "INE" }
    return [
      { field: "RFC", rule: "custom:mi_regla", valid: true, message: "OK" }
    ];
  }
`);
}

const TEMPLATE = (name) => `/**
 * Custom validator: ${name}
 *
 * Este script se ejecuta cuando un tipo documental tiene
 * "custom_validator": "${name}" en su configuración.
 *
 * Se ejecuta DESPUÉS de las validaciones estándar (regex, fechas, presencia).
 * Úsalo para lógica que requiere:
 *   - Consultas a APIs externas
 *   - Cálculos numéricos complejos
 *   - Cruces entre campos
 *   - Integraciones con sistemas internos
 */

/**
 * @param {Record<string, string>} entityMap - Entidades extraídas del documento
 * @param {{ documentId: string, classification: string }} context - Contexto del procesamiento
 * @returns {Promise<Array<{field: string, rule: string, valid: boolean, message: string}>>}
 */
export async function validate(entityMap, context) {
  const checks = [];

  // ─── Tu lógica de validación aquí ───────────────────────────────
  //
  // Ejemplo: Validar RFC contra un servicio externo
  //
  // const rfc = entityMap["RFC"];
  // if (rfc) {
  //   try {
  //     const response = await fetch(\`https://api.ejemplo.com/rfc/\${rfc}\`);
  //     const data = await response.json();
  //     checks.push({
  //       field: "RFC",
  //       rule: "custom:${name}",
  //       valid: data.exists === true,
  //       message: data.exists
  //         ? "RFC verificado en sistema externo"
  //         : "RFC no encontrado en sistema externo"
  //     });
  //   } catch (err) {
  //     checks.push({
  //       field: "RFC",
  //       rule: "custom:${name}",
  //       valid: false,
  //       message: \`Error consultando servicio: \${err.message}\`
  //     });
  //   }
  // }
  //
  // ────────────────────────────────────────────────────────────────

  return checks;
}
`;

function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args.help) {
    printHelp();
    process.exit(0);
  }

  if (!args.name) {
    console.error("❌ Error: --name es requerido\n");
    printHelp();
    process.exit(1);
  }

  const name = args.name.replace(/\.mjs$/, "");
  const filePath = resolve(VALIDATORS_DIR, `${name}.mjs`);

  if (existsSync(filePath)) {
    console.error(`❌ El validator '${name}.mjs' ya existe en:`);
    console.error(`   ${filePath}`);
    process.exit(1);
  }

  // Create the validator file
  writeFileSync(filePath, TEMPLATE(name), "utf8");
  console.log(`\n✅ Custom validator creado: custom_validators/${name}.mjs\n`);

  // Optionally associate with a document type
  if (args.type) {
    const typeId = args.type.toUpperCase().replace(/\s+/g, "_");
    try {
      const config = JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
      if (config.types[typeId]) {
        config.types[typeId].custom_validator = name;
        writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2) + "\n", "utf8");
        console.log(`   ✅ Asociado al tipo '${typeId}' en el catálogo.\n`);
      } else {
        console.warn(`   ⚠️  El tipo '${typeId}' no existe en el catálogo.`);
        console.warn(`   Agrégalo primero con: pnpm add-type --type ${typeId} ...\n`);
      }
    } catch {
      console.warn(`   ⚠️  No se pudo actualizar el catálogo automáticamente.\n`);
    }
  }

  console.log(`Próximos pasos:`);
  console.log(`  1. Edita la función validate() en custom_validators/${name}.mjs`);
  if (!args.type) {
    console.log(`  2. Asocia al tipo en config: "custom_validator": "${name}"`);
    console.log(`  3. Redespliega: pnpm build && npx cdk deploy`);
  } else {
    console.log(`  2. Redespliega: pnpm build && npx cdk deploy`);
  }
  console.log();
}

main();
