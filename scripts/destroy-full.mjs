#!/usr/bin/env node
/**
 * Destroy completo: elimina el stack CDK Y la tabla DynamoDB del catálogo.
 *
 * El destroy normal (pnpm destroy) NO borra la tabla DynamoDB porque usa
 * Table.fromTableName (importada, no gestionada por el stack).
 *
 * Este script:
 * 1. Pide confirmación explícita
 * 2. Destruye el stack CDK
 * 3. Elimina la tabla DynamoDB del catálogo
 *
 * Uso:
 *   pnpm destroy:full
 *   pnpm destroy:full --table document-validation-catalog --region us-east-1
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
      resolve(answer.trim().toLowerCase());
    });
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const table = args.table || "document-validation-catalog";
  const region = args.region || process.env.AWS_REGION || "us-east-1";

  console.log(`\n⚠️  DESTROY COMPLETO`);
  console.log(`   Esto eliminará:`);
  console.log(`   - El stack CDK (DocumentValidationStack)`);
  console.log(`   - La tabla DynamoDB: ${table}`);
  console.log(`   - Todos los datos del catálogo en DynamoDB`);
  console.log(`\n   Esta acción NO se puede deshacer.\n`);

  const answer = await confirm(`   Escribe "ELIMINAR TODO" para confirmar: `);

  if (answer !== "eliminar todo") {
    console.log("\n   Cancelado.\n");
    process.exit(0);
  }

  // 1. Destroy CDK stack
  console.log("\n🗑️  Destruyendo stack CDK...");
  try {
    execSync("npx cdk destroy --force", { stdio: "inherit", timeout: 300000 });
  } catch (e) {
    console.error("⚠️  Error destruyendo stack (puede que ya no exista)");
  }

  // 2. Delete DynamoDB table
  console.log(`\n🗑️  Eliminando tabla DynamoDB '${table}'...`);
  try {
    execSync(
      `aws dynamodb delete-table --table-name "${table}" --region ${region}`,
      { stdio: "inherit", timeout: 30000 }
    );
    console.log(`\n✅ Tabla '${table}' eliminada.`);
  } catch (e) {
    console.error(`⚠️  No se pudo eliminar la tabla (puede que no exista).`);
  }

  console.log("\n✅ Destroy completo finalizado.\n");
}

main();
