#!/usr/bin/env node
/**
 * CLI: Previsualizar los prompts que se generarán para Bedrock.
 *
 * Uso:
 *   pnpm preview-prompts
 *   pnpm preview-prompts --classification   (solo prompt de clasificación)
 *   pnpm preview-prompts --extraction       (solo prompt de extracción)
 *
 * Requiere haber hecho build primero: pnpm build
 */

import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import { createRequire } from "module";

const __dirname = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

function parseArgs(args) {
  const parsed = {};
  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith("--")) {
      parsed[args[i].slice(2)] = true;
    }
  }
  return parsed;
}

function main() {
  const args = parseArgs(process.argv.slice(2));

  let mod;
  try {
    mod = require(resolve(__dirname, "../dist/prompt-generator.js"));
  } catch (e) {
    console.error("❌ No se encontró dist/prompt-generator.js");
    console.error("   Ejecuta primero: pnpm build\n");
    process.exit(1);
  }

  const showClassification = !args.extraction;
  const showExtraction = !args.classification;

  if (showClassification) {
    console.log("═".repeat(80));
    console.log(" PROMPT DE CLASIFICACIÓN");
    console.log("═".repeat(80));
    console.log(mod.generateClassificationPrompt());
  }

  if (showExtraction) {
    console.log("═".repeat(80));
    console.log(" PROMPT DE EXTRACCIÓN (con hints)");
    console.log("═".repeat(80));
    console.log(mod.generateProcessingPrompt());
  }
}

main();
