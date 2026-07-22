#!/usr/bin/env node
/**
 * Elimina (o deshabilita) tipos documentales del catálogo DynamoDB.
 *
 * Al borrar un tipo de DynamoDB, el runtime vuelve automáticamente a la
 * versión del JSON local empaquetado con la Lambda (si existe ahí).
 *
 * Uso:
 *   pnpm db-delete-type --type PEDIMENTO_ADUANAL            # borrar un tipo
 *   pnpm db-delete-type --type INE --disable                # solo deshabilitar (enabled=false)
 *   pnpm db-delete-type --all --yes                         # vaciar TODO el catálogo DynamoDB
 *
 * Opciones:
 *   --type       Type ID a borrar/deshabilitar
 *   --disable    No borra: marca enabled=false (reversible con db-sync)
 *   --all        Borra todos los tipos de la tabla (pide --yes)
 *   --yes        Confirma operaciones destructivas sin preguntar
 *   --table      Tabla DynamoDB (default: CATALOG_TABLE env o "document-validation-catalog")
 *   --region     AWS region (default: AWS_REGION env o "us-east-1")
 */

import { execSync } from "child_process";
import { createInterface } from "readline";

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

async function confirm(message) {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((resolve) => {
    rl.question(message, (answer) => {
      rl.close();
      resolve(answer.trim().toLowerCase() === "si" || answer.trim().toLowerCase() === "s");
    });
  });
}

function aws(cmd, region) {
  return execSync(`aws ${cmd} --region ${region}`, { stdio: "pipe", timeout: 30000 }).toString();
}

function listTypeIds(table, region) {
  const out = aws(
    `dynamodb scan --table-name "${table}" --projection-expression type_id --output json`,
    region
  );
  return JSON.parse(out).Items.map((i) => i.type_id.S);
}

function deleteType(typeId, table, region) {
  aws(
    `dynamodb delete-item --table-name "${table}" --key '{"type_id":{"S":"${typeId}"}}'`,
    region
  );
}

function disableType(typeId, table, region) {
  aws(
    `dynamodb update-item --table-name "${table}" --key '{"type_id":{"S":"${typeId}"}}' ` +
      `--update-expression "SET enabled = :f" --expression-attribute-values '{":f":{"BOOL":false}}'`,
    region
  );
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const table = args.table || process.env.CATALOG_TABLE || "document-validation-catalog";
  const region = args.region || process.env.AWS_REGION || "us-east-1";

  if (args.help || (!args.type && !args.all)) {
    console.log("Uso: pnpm db-delete-type --type TYPE_ID [--disable] | --all [--yes]");
    console.log("Ver encabezado del script para detalles.");
    process.exit(args.help ? 0 : 1);
  }

  if (args.all) {
    const ids = listTypeIds(table, region);
    if (ids.length === 0) {
      console.log(`La tabla '${table}' ya está vacía.`);
      return;
    }
    console.log(`Se borrarán ${ids.length} tipos de '${table}': ${ids.join(", ")}`);
    const ok = args.yes || (await confirm("¿Confirmas? (si/no): "));
    if (!ok) { console.log("Cancelado."); return; }
    for (const id of ids) {
      deleteType(id, table, region);
      console.log(`  🗑️  ${id}`);
    }
    console.log("Catálogo DynamoDB vacío. El runtime usa ahora solo el JSON local.");
    return;
  }

  if (args.disable) {
    disableType(args.type, table, region);
    console.log(`⏸️  '${args.type}' deshabilitado (enabled=false). Reversible con pnpm db-sync.`);
  } else {
    deleteType(args.type, table, region);
    console.log(`🗑️  '${args.type}' eliminado de DynamoDB. Si existe en el JSON local, esa versión vuelve a aplicar.`);
  }
}

main().catch((e) => {
  console.error(`❌ ${e.stderr?.toString().trim() || e.message}`);
  process.exit(1);
});
