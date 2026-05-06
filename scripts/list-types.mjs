#!/usr/bin/env node
/**
 * CLI: Listar tipos documentales del JSON config local.
 *
 * Uso:
 *   pnpm list-types
 */

import { readFileSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = resolve(
  __dirname,
  "../src/document-processing/resources/enrichment-lambda/config/document_types.json"
);

function main() {
  const config = JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
  const types = config.types;

  console.log(`\n📋 Tipos documentales en config local:\n`);

  for (const [typeId, data] of Object.entries(types)) {
    const fields = data.extraction_fields || [];
    const required = data.required_fields || [];
    const rules = data.validation_rules || [];
    const hints = data.extraction_hints || {};
    const desc = data.description || "";

    console.log(`   📄 ${typeId}${desc ? ` - ${desc}` : ""}`);
    console.log(`      Campos:     ${fields.length} (${fields.slice(0, 4).join(", ")}${fields.length > 4 ? "..." : ""})`);
    console.log(`      Requeridos: ${required.join(", ") || "(ninguno)"}`);
    console.log(`      Reglas:     ${rules.length} validación(es)`);
    console.log(`      Hints:      ${Object.keys(hints).length} pista(s) de extracción`);
    if (data.validator_module) console.log(`      Validator:  ${data.validator_module}`);
    if (data.custom_validator) console.log(`      Custom:     ${data.custom_validator}.mjs`);
    console.log();
  }

  console.log(`   Total: ${Object.keys(types).length} tipo(s)\n`);
}

main();
