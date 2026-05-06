#!/usr/bin/env node
/**
 * CLI: Listar tipos documentales en DynamoDB.
 *
 * Uso:
 *   pnpm db-list-types
 *   pnpm db-list-types --table mi-tabla --region us-west-2
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

function main() {
  const args = parseArgs(process.argv.slice(2));
  const table = args.table || process.env.CATALOG_TABLE || "document-validation-catalog";
  const region = args.region || process.env.AWS_REGION || "us-east-1";

  console.log(`\n📋 Tipos documentales en DynamoDB (${table} @ ${region}):\n`);

  try {
    const result = execSync(
      `aws dynamodb scan --table-name "${table}" --region ${region} --output json`,
      { encoding: "utf8" }
    );
    const data = JSON.parse(result);
    const items = data.Items || [];

    if (items.length === 0) {
      console.log("   (tabla vacía - no hay tipos en DynamoDB)\n");
      return;
    }

    for (const item of items) {
      const typeId = item.type_id?.S || "?";
      const desc = item.description?.S || "";
      const enabled = item.enabled?.BOOL !== false;
      const fields = (item.extraction_fields?.L || []).map(f => f.S).join(", ");
      const rulesCount = (item.validation_rules?.L || []).length;

      console.log(`   ${enabled ? "✅" : "❌"} ${typeId}${desc ? ` - ${desc}` : ""}`);
      console.log(`      Campos: ${fields || "(ninguno)"}`);
      console.log(`      Reglas: ${rulesCount}`);
      console.log();
    }

    console.log(`   Total: ${items.length} tipo(s)\n`);
  } catch (e) {
    console.error(`❌ Error consultando DynamoDB.`);
    console.error(`   Verifica que la tabla '${table}' existe y tienes permisos.\n`);
    process.exit(1);
  }
}

main();
