#!/usr/bin/env node
/**
 * Sync: Sube un tipo documental a DynamoDB (crea o sobrescribe).
 *
 * Acepta un JSON inline o un archivo JSON. DynamoDB siempre gana sobre el local.
 *
 * Uso:
 *   # Desde un archivo JSON
 *   pnpm db-sync --file catalogs/curp.json
 *
 *   # Inline JSON (el key del objeto es el type_id)
 *   pnpm db-sync --json '{"CURP":{"description":"...","extraction_fields":[...],...}}'
 *
 *   # Sincronizar TODOS los tipos del JSON local a DynamoDB
 *   pnpm db-sync --all
 *
 * Opciones:
 *   --file       Ruta a un archivo JSON con el tipo (key = type_id)
 *   --json       JSON inline con el tipo (key = type_id)
 *   --all        Sincronizar todos los tipos del config local a DynamoDB
 *   --table      Tabla DynamoDB (default: CATALOG_TABLE env o "document-validation-catalog")
 *   --region     AWS region (default: AWS_REGION env o "us-east-1")
 *   --help       Mostrar ayuda
 */

import { readFileSync, writeFileSync, existsSync, unlinkSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import { execSync } from "child_process";

const __dirname = dirname(fileURLToPath(import.meta.url));
const LOCAL_CONFIG = resolve(
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
Uso: pnpm db-sync [opciones]

Sube tipos documentales a DynamoDB (crea o sobrescribe).
DynamoDB siempre tiene prioridad sobre el JSON local en runtime.

Opciones:
  --file       Archivo JSON con tipos (key = type_id)
  --json       JSON inline con tipos (key = type_id)
  --all        Sincronizar todos los tipos del config local
  --table      Tabla DynamoDB (default: "document-validation-catalog")
  --region     AWS region (default: "us-east-1")

Formato del JSON:
  {
    "CURP": {
      "description": "Constancia de CURP",
      "extraction_fields": ["CURP", "NOMBRE_COMPLETO", ...],
      "extraction_hints": {"CURP": "Clave de 18 chars en la parte superior"},
      "required_fields": ["CURP", "NOMBRE_COMPLETO"],
      "validation_rules": [{"field": "CURP", "rule_type": "regex", "pattern": "..."}],
      "validator_module": "validators.identification"
    }
  }

Ejemplos:
  pnpm db-sync --file catalogs/curp.json
  pnpm db-sync --json '{"CURP":{...}}'
  pnpm db-sync --all
`);
}

function putType(typeId, config, table, region) {
  const item = {
    type_id: { S: typeId },
    description: { S: config.description || "" },
    extraction_fields: { L: (config.extraction_fields || []).map((f) => ({ S: f })) },
    required_fields: { L: (config.required_fields || []).map((f) => ({ S: f })) },
    enabled: { BOOL: true },
  };

  // Validation rules
  if (config.validation_rules && config.validation_rules.length > 0) {
    item.validation_rules = {
      L: config.validation_rules.map((r) => {
        const m = {
          field: { S: r.field || "" },
          rule_type: { S: r.rule_type || "" },
        };
        if (r.pattern) m.pattern = { S: r.pattern };
        if (r.message) m.message = { S: r.message };
        return { M: m };
      }),
    };
  } else {
    item.validation_rules = { L: [] };
  }

  // Extraction hints
  if (config.extraction_hints && Object.keys(config.extraction_hints).length > 0) {
    const hintsMap = {};
    for (const [k, v] of Object.entries(config.extraction_hints)) {
      hintsMap[k] = { S: v };
    }
    item.extraction_hints = { M: hintsMap };
  }

  // Validator module
  if (config.validator_module) {
    item.validator_module = { S: config.validator_module };
  }

  // Custom validator
  if (config.custom_validator) {
    item.custom_validator = { S: config.custom_validator };
  }

  const itemJson = JSON.stringify(item);

  // Write to temp file to avoid shell escaping issues with quotes/commas
  const tmpFile = `/tmp/dynamodb-item-${typeId}-${Date.now()}.json`;
  writeFileSync(tmpFile, itemJson);

  try {
    execSync(
      `aws dynamodb put-item --table-name "${table}" --item file://${tmpFile} --region ${region}`,
      { stdio: "pipe", timeout: 15000 }
    );
    unlinkSync(tmpFile);
    return true;
  } catch (e) {
    try { unlinkSync(tmpFile); } catch {}
    console.error(`  ❌ Error subiendo '${typeId}': ${e.stderr?.toString().trim() || e.message}`);
    return false;
  }
}

function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args.help) { printHelp(); process.exit(0); }

  const table = args.table || process.env.CATALOG_TABLE || "document-validation-catalog";
  const region = args.region || process.env.AWS_REGION || "us-east-1";

  let types = {};

  // Load types from source
  if (args.file) {
    if (!existsSync(args.file)) {
      console.error(`❌ Archivo no encontrado: ${args.file}`);
      process.exit(1);
    }
    try {
      const content = JSON.parse(readFileSync(args.file, "utf8"));
      // Support both {TYPE: {...}} and {types: {TYPE: {...}}}
      types = content.types || content;
    } catch (e) {
      console.error(`❌ JSON inválido en ${args.file}: ${e.message}`);
      process.exit(1);
    }
  } else if (args.json) {
    try {
      types = JSON.parse(args.json);
    } catch (e) {
      console.error(`❌ JSON inline inválido: ${e.message}`);
      process.exit(1);
    }
  } else if (args.all) {
    try {
      const local = JSON.parse(readFileSync(LOCAL_CONFIG, "utf8"));
      types = local.types || {};
    } catch (e) {
      console.error(`❌ No se pudo leer config local: ${e.message}`);
      process.exit(1);
    }
  } else {
    console.error("❌ Especifica --file, --json, o --all\n");
    printHelp();
    process.exit(1);
  }

  const typeIds = Object.keys(types);
  if (typeIds.length === 0) {
    console.error("❌ No se encontraron tipos en el JSON proporcionado.");
    process.exit(1);
  }

  console.log(`\n📤 Sincronizando ${typeIds.length} tipo(s) a DynamoDB (${table} @ ${region})...\n`);

  let success = 0;
  let failed = 0;

  for (const typeId of typeIds) {
    const ok = putType(typeId, types[typeId], table, region);
    if (ok) {
      console.log(`  ✅ ${typeId} → sobrescrito/creado`);
      success++;
    } else {
      failed++;
    }
  }

  console.log(`\n   Resultado: ${success} exitoso(s), ${failed} fallido(s)`);
  if (success > 0) {
    console.log(`   ⚡ Efecto inmediato - no requiere redeploy.\n`);
  }
}

main();
