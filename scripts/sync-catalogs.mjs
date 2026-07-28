#!/usr/bin/env node
/**
 * Build-time bundling: combine the per-type catalog files in `catalogs/` into a
 * single `config/catalogs.json` that ships inside the Lambda.
 *
 * `catalogs/` is the ONE source of truth:
 *   - catalogs/_shared.json  → { _field_sets, cross_document_validation_rules,
 *                                _global_output_fields, _common_item_fields }
 *                                (shared globals; _common_item_fields is the
 *                                field-centric DSL's reusable item_fields, see
 *                                catalogs/_TEMPLATE.json)
 *   - catalogs/_TEMPLATE.json → template for new types (ignored here)
 *   - catalogs/<TYPE_ID>.json → one document type, shape { "<TYPE_ID>": { ... } }
 *
 * Output: config/catalogs.json in the customs shape the loaders understand:
 *   { _global_output_fields, _field_sets, _common_item_fields, document_types,
 *     cross_document_validation_rules }
 *
 * Runs as part of `pnpm build`. Editing a type in `catalogs/` and rebuilding is
 * all that is needed to bake it into the deployed Lambda.
 */

import { readFileSync, writeFileSync, readdirSync, existsSync, rmSync } from "fs";
import { resolve, dirname, join } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repo = resolve(__dirname, "..");
const CATALOGS_DIR = join(repo, "catalogs");
const CONFIG_DIR = join(
  repo,
  "src/document-processing/resources/enrichment-lambda/config"
);
const OUTPUT = join(CONFIG_DIR, "catalogs.json");
// Lista ligera de tipos (id + nombre + alias) que se empaqueta con el query
// Lambda para resolver `?docType=pedimento aduanal` → PEDIMENTO_ADUANAL sin
// leer el catálogo completo.
const TYPES_OUTPUT = join(
  repo,
  "src/document-processing/resources/query-lambda/document-types.json"
);

// Files that are not a document type.
const SPECIAL = new Set(["_shared.json", "_TEMPLATE.json"]);

function loadJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

/** "PEDIMENTO_ADUANAL" -> "Pedimento Aduanal" (nombre por defecto del tipo). */
function titleize(typeId) {
  return typeId
    .split("_")
    .filter(Boolean)
    .map((w) => w.charAt(0) + w.slice(1).toLowerCase())
    .join(" ");
}

/**
 * Escribe la lista de tipos que el query Lambda usa para resolver el parámetro
 * `docType` cuando viene como nombre en vez de TYPE_ID. `display_name` y
 * `aliases` son opcionales en cada entrada del catálogo.
 */
function writeTypeIndex(documentTypes) {
  const types = Object.entries(documentTypes).map(([typeId, entry]) => ({
    typeId,
    displayName: entry.display_name || titleize(typeId),
    aliases: entry.aliases || [],
  }));
  writeFileSync(
    TYPES_OUTPUT,
    JSON.stringify({ types }, null, 2) + "\n",
    "utf8"
  );
  console.log(`  índice de tipos (${types.length}) -> ${TYPES_OUTPUT}`);
}

function main() {
  const shared = existsSync(join(CATALOGS_DIR, "_shared.json"))
    ? loadJson(join(CATALOGS_DIR, "_shared.json"))
    : {};

  const documentTypes = {};
  const files = readdirSync(CATALOGS_DIR)
    .filter((f) => f.endsWith(".json") && !SPECIAL.has(f))
    .sort();

  for (const file of files) {
    const data = loadJson(join(CATALOGS_DIR, file));
    // Each type file is { "<TYPE_ID>": { ...entry } }.
    for (const [typeId, entry] of Object.entries(data)) {
      if (typeId.startsWith("_")) continue;
      if (documentTypes[typeId]) {
        console.warn(`  ⚠ tipo duplicado '${typeId}' (${file}) — se sobrescribe`);
      }
      documentTypes[typeId] = entry;
    }
  }

  const combined = {
    _global_output_fields: shared._global_output_fields || [],
    _field_sets: shared._field_sets || {},
    _common_item_fields: shared._common_item_fields || {},
    document_types: documentTypes,
    cross_document_validation_rules: shared.cross_document_validation_rules || [],
  };

  writeFileSync(OUTPUT, JSON.stringify(combined, null, 2), "utf8");
  console.log(
    `bundled ${Object.keys(documentTypes).length} tipos de ${CATALOGS_DIR}/*.json -> ${OUTPUT}`
  );

  writeTypeIndex(documentTypes);

  // Remove superseded bundled configs (single base = catalogs.json).
  for (const stale of ["document_types.json", "customs_catalog.json"]) {
    const p = join(CONFIG_DIR, stale);
    if (existsSync(p)) {
      rmSync(p);
      console.log(`  removed obsolete ${stale}`);
    }
  }
}

main();
