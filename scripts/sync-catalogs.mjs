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
 *   - recursive <TYPE_ID>.json files → one document type, except any `save/` subtree
 *
 * Output: config/catalogs.json in the customs shape the loaders understand:
 *   { _global_output_fields, _field_sets, _common_item_fields, document_types,
 *     cross_document_validation_rules }
 *
 * Runs as part of `pnpm build`. Editing a type in `catalogs/` and rebuilding is
 * all that is needed to bake it into the deployed Lambda.
 */

import { readFileSync, writeFileSync, existsSync, rmSync } from "fs";
import { resolve, dirname, join } from "path";
import { fileURLToPath } from "url";
import { uniqueKeysOf } from "./_unique_keys.mjs";
import { discoverCatalogTypes } from "./catalog-files.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repo = resolve(__dirname, "..");
const CATALOGS_DIR = join(repo, "catalogs");
const CONFIG_DIR = join(
  repo,
  "src/document-processing/resources/enrichment-lambda/config"
);
const OUTPUT = join(CONFIG_DIR, "catalogs.json");
// Índice de tipos que se empaqueta con el query Lambda: resuelve
// `?docType=pedimento aduanal` → PEDIMENTO_ADUANAL sin leer el catálogo
// completo (id + nombre + alias), y además sirve de respaldo bundled para
// `GET /document-types` (id + nombre + descripción + `fields` = schema) —
// mismo archivo, dos usos; ver document-type-catalog.mjs.
const TYPES_OUTPUT = join(
  repo,
  "src/document-processing/resources/query-lambda/document-types.json"
);

function loadJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function validateOccurrenceMode(typeId, entry, file) {
  if (!("occurrence_mode" in entry)) return;
  if (!new Set(["single", "multiple"]).has(entry.occurrence_mode)) {
    throw new Error(
      `${file}: ${typeId}.occurrence_mode debe ser "single" o "multiple"`
    );
  }
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
 * Escribe la lista de tipos que el query Lambda usa para (a) resolver el
 * parámetro `docType` cuando viene como nombre en vez de TYPE_ID, y (b)
 * responder `GET /document-types` como respaldo bundled cuando un tipo aún no
 * se sincronizó a DynamoDB (o el catálogo hot-reload no está disponible) —
 * mismo archivo para los dos, ver `document-type-catalog.mjs`. `display_name`
 * y `description` son opcionales en cada entrada del catálogo.
 *
 * Lleva también las unique keys declaradas por cada tipo (`uniqueKeys`), que
 * son lo que le permite al query Lambda reconocer que `field=NUMERO_PEDIMENTO`
 * se resuelve con una Query a `TIPODOC#valor` en vez de recorriendo el tipo
 * entero — ver `_unique_keys.mjs` y `enrichment-lambda/unique_keys.py`. Solo
 * se emite cuando el tipo declara alguna, así el archivo no crece para los
 * tipos que no la usan.
 *
 * `fields` va tal cual (el árbol DSL field-centric completo, o `null` para un
 * tipo legacy-DSL que no lo declara) — es el "schema" que expone
 * `GET /document-types`, deliberadamente sin recortar ni traducir: la fuente
 * de verdad de esa forma sigue siendo `catalogs/README.md`.
 */
function writeTypeIndex(documentTypes) {
  const types = Object.entries(documentTypes).map(([typeId, entry]) => {
    const uniqueKeys = uniqueKeysOf(entry);
    return {
      typeId,
      displayName: entry.display_name || titleize(typeId),
      description: entry.description || null,
      aliases: entry.aliases || [],
      ...(uniqueKeys.length && { uniqueKeys }),
      fields: entry.fields || null,
    };
  });
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
  const types = discoverCatalogTypes(CATALOGS_DIR);
  for (const { typeId, entry, relativePath } of types) {
    validateOccurrenceMode(typeId, entry, relativePath);
    documentTypes[typeId] = entry;
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
    `bundled ${Object.keys(documentTypes).length} tipos activos de ${CATALOGS_DIR}/**/*.json -> ${OUTPUT}`
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
