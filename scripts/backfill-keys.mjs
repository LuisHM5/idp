#!/usr/bin/env node
/**
 * Backfill the unique-key lookup table from the stored EnrichmentResults.
 *
 * The enrichment Lambda writes one lookup row per unique key a type declares
 * (`enrichment-lambda/unique_keys.py`), keyed `TIPODOC#valor`, which is what
 * lets `GET /documents?docType=&field=&value=` answer with a single Query.
 * Documents processed BEFORE the type declared its unique key — or before the
 * table existed — have no row, so a search for them silently takes the slow
 * per-type walk instead. This script rebuilds the rows from the envelopes
 * already stored on the processing table.
 *
 * Run it after adding or changing a `unique_key` in `catalogs/` (and after
 * `pnpm build`, which is what refreshes the declarations this script reads).
 *
 * Usage:
 *   pnpm backfill:keys                        # tabla del stack, todos los items
 *   pnpm backfill:keys -- --dry-run           # solo muestra lo que escribiría
 *   pnpm backfill:keys -- --type PEDIMENTO_ADUANAL   # solo ese tipo
 *   pnpm backfill:keys -- --table NAME --keys-table NAME --region us-east-1
 *
 * Idempotent: rows are keyed (UniqueKey, DocumentId), so re-running overwrites
 * rather than duplicating. It never DELETES: a value that changed leaves its
 * old row behind, pointing at a document that no longer carries it — the
 * search still returns the document, and the response's `matchedValue` shows
 * what was actually found. Pass `--prune` to drop rows for (documentId, type)
 * pairs this run re-derived, before writing the new ones.
 *
 * Uses the AWS CLI (same approach as scripts/backfill-index.mjs) so it needs
 * no extra npm dependency — just credentials with dynamodb:Scan/BatchWriteItem.
 */

import { readFileSync, writeFileSync, unlinkSync } from "fs";
import { execSync } from "child_process";
import { buildUniqueKey } from "./_unique_keys.mjs";
import { readDocuments } from "../src/document-processing/resources/query-lambda/envelope-shape.mjs";

const STACK = process.env.STACK_NAME || "DocumentValidationStack";
/** DynamoDB caps a BatchWriteItem at 25 requests. */
const BATCH_SIZE = 25;
/** Same cap enrichment-lambda/unique_keys.py applies per document. */
const MAX_KEYS_PER_DOCUMENT = 200;

function parseArgs(argv) {
  const opts = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--" || !a.startsWith("--")) continue;
    const key = a.slice(2);
    if (["dry-run", "prune"].includes(key)) opts[key] = true;
    else if (i + 1 < argv.length && !argv[i + 1].startsWith("--")) opts[key] = argv[++i];
    else opts[key] = true;
  }
  return opts;
}

function aws(args, region) {
  return execSync(`aws ${args} --region ${region} --output json`, {
    maxBuffer: 256 * 1024 * 1024,
    stdio: ["ignore", "pipe", "pipe"],
  }).toString();
}

/** Locate a table of the deployed stack by its logical-id fragment. */
function resolveTable(region, fragment, flag) {
  const out = aws(
    `cloudformation describe-stack-resources --stack-name ${STACK} ` +
      `--query "StackResources[?ResourceType=='AWS::DynamoDB::Table'].PhysicalResourceId"`,
    region
  );
  const tables = JSON.parse(out).filter((t) => t.includes(fragment));
  if (!tables.length) {
    throw new Error(`No encontré la tabla '${fragment}' en el stack ${STACK}; pásala con ${flag}`);
  }
  return tables[0];
}

/**
 * Unique keys declared per type, as baked into the query Lambda's type index
 * by `pnpm build` (scripts/sync-catalogs.mjs). Regenerate it before running
 * this script if you just edited a catalog.
 */
function uniqueKeysByType() {
  const file = new URL(
    "../src/document-processing/resources/query-lambda/document-types.json",
    import.meta.url
  );
  let types = [];
  try {
    ({ types = [] } = JSON.parse(readFileSync(file, "utf8")));
  } catch {
    throw new Error(
      "No encontré document-types.json — corre `pnpm build` antes de este script."
    );
  }
  const byType = new Map();
  for (const t of types) {
    if (t.uniqueKeys && t.uniqueKeys.length) byType.set(t.typeId, t.uniqueKeys);
  }
  return byType;
}

/** Flat {FIELD: value} de un mapa de campos, en cualquiera de sus formas
 * (nodo `{value, ...}` de v2, o valor pelón de v1) — espejo de
 * `enrichment-lambda/unique_keys.py::_values`. */
function values(entities) {
  const out = {};
  for (const [name, node] of Object.entries(entities || {})) {
    if (Array.isArray(node)) continue;
    if (node && typeof node === "object") {
      if ("value" in node) out[name] = node.value;
    } else {
      out[name] = node;
    }
  }
  return out;
}


/**
 * La clasificación reducida a UN tipo, espejo de
 * `enrichment-lambda/pipeline/envelope.py::index_type_of`.
 *
 * `documentClassification` puede traer varios tipos separados por coma (un
 * expediente mixto). La columna `DocumentType` no puede: es la partition key de
 * un GSI y una Query busca por valor EXACTO, así que un backfill que escribiera
 * "A,B" ahí dejaría esos expedientes invisibles para `?docType=A`.
 */
function indexTypeOf(classification) {
  return String(classification || "").includes(",")
    ? "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO"
    : classification;
}

/** Mirror of `enrichment-lambda/unique_keys.py::derive_unique_keys`. */
function deriveRows(envelope, specsByType, indexedAt) {
  const documentId = envelope.documentId;
  if (!documentId || documentId === "unknown") return [];

  const classification = indexTypeOf(
    String(envelope.documentClassification || "OTRO").trim().toUpperCase()
  );
  const expediente = envelope.expediente || {};
  // Este script relee envelopes guardados de CUALQUIER edad, así que pasa por
  // el normalizador en vez de conocer una sola forma (ver envelope-shape.mjs).
  const docs = readDocuments(envelope);
  const sources = docs.length
    ? docs
        .map((d) => ({
          type: String(d.documentType || "").trim().toUpperCase(),
          values: values(d.fields),
        }))
        .filter((sourceDoc) => sourceDoc.type && sourceDoc.type !== "OTRO")
        .map((sourceDoc) => ({
          ...sourceDoc,
          container: classification !== sourceDoc.type ? classification : null,
        }))
    : [{ type: classification, values: values(envelope.extractedEntities), container: null }];

  const processedAt = String(envelope.processedAt || expediente.processedAt || "");
  const status = String((envelope.validation || {}).status || "REQUIERE_REVISION");
  const documentName = String(envelope.sourceFile || expediente.sourceFile || "").split("/").pop();

  const rows = [];
  const seen = new Set();
  for (const source of sources) {
    for (const spec of specsByType.get(source.type) || []) {
      const raw = source.values[spec.field];
      const key = buildUniqueKey(source.type, raw, spec.normalize);
      if (!key || seen.has(key) || rows.length >= MAX_KEYS_PER_DOCUMENT) continue;
      seen.add(key);
      rows.push({
        UniqueKey: key,
        DocumentId: documentId,
        DocumentType: source.type,
        FieldName: spec.field,
        Value: String(raw),
        NormalizedValue: key.slice(key.indexOf("#") + 1),
        ProcessedAt: processedAt || indexedAt,
        ValidationStatus: status,
        IndexedAt: indexedAt,
        ...(source.container && { ContainerType: source.container }),
        ...(documentName && { DocumentName: documentName }),
      });
    }
  }
  return rows;
}

/** Low-level DynamoDB JSON for a flat string map (every attribute here is a
 * string — see the table shape in `unique_keys.py`). */
function toItem(row) {
  const item = {};
  for (const [k, v] of Object.entries(row)) if (v !== "" && v != null) item[k] = { S: String(v) };
  return item;
}

function batchWrite(table, rows, region, remove = false) {
  for (let i = 0; i < rows.length; i += BATCH_SIZE) {
    const chunk = rows.slice(i, i + BATCH_SIZE);
    const payload = {
      RequestItems: {
        [table]: chunk.map((row) =>
          remove
            ? { DeleteRequest: { Key: { UniqueKey: { S: row.UniqueKey }, DocumentId: { S: row.DocumentId } } } }
            : { PutRequest: { Item: toItem(row) } }
        ),
      },
    };
    const tmp = `/tmp/ddb-keys-${Date.now()}-${Math.random().toString(36).slice(2)}.json`;
    writeFileSync(tmp, JSON.stringify(payload));
    try {
      aws(`dynamodb batch-write-item --cli-input-json file://${tmp}`, region);
    } finally {
      unlinkSync(tmp);
    }
  }
}

/** Rows already stored for a document, so --prune can drop the stale ones. */
function existingRows(keysTable, documentIds, region) {
  // No GSI on DocumentId (the lookup table exists to be read by key, not by
  // document), so pruning scans it once and filters locally. Only paid for
  // when --prune is passed.
  const wanted = new Set(documentIds);
  const rows = [];
  let startKey = null;
  do {
    const page = JSON.parse(
      aws(
        `dynamodb scan --table-name ${keysTable} --no-paginate ` +
          `--projection-expression "UniqueKey, DocumentId" ` +
          (startKey ? `--exclusive-start-key '${JSON.stringify(startKey)}' ` : ""),
        region
      )
    );
    for (const item of page.Items || []) {
      const documentId = item.DocumentId && item.DocumentId.S;
      if (wanted.has(documentId)) {
        rows.push({ UniqueKey: item.UniqueKey.S, DocumentId: documentId });
      }
    }
    startKey = page.LastEvaluatedKey || null;
  } while (startKey);
  return rows;
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const region = opts.region || process.env.AWS_REGION || "us-east-1";
  const dryRun = !!opts["dry-run"];
  const onlyType = opts.type ? String(opts.type).trim().toUpperCase() : null;
  const table = opts.table || resolveTable(region, "DocumentProcessingTable", "--table");
  const keysTable = opts["keys-table"] || resolveTable(region, "UniqueKeyTable", "--keys-table");

  let specsByType = uniqueKeysByType();
  if (onlyType) {
    specsByType = new Map(specsByType.has(onlyType) ? [[onlyType, specsByType.get(onlyType)]] : []);
  }
  if (!specsByType.size) {
    console.log(
      onlyType
        ? `⚠ El tipo ${onlyType} no declara ninguna unique key — nada que reconstruir.`
        : "⚠ Ningún tipo del catálogo declara unique keys — nada que reconstruir."
    );
    return;
  }

  console.log(`🔑 Backfill de unique keys ${table} -> ${keysTable} (${region})`);
  console.log(
    `   tipos con unique key: ${[...specsByType.keys()].join(", ")}` +
      (dryRun ? "\n   modo dry-run: no se escribe nada" : "")
  );

  const indexedAt = new Date().toISOString();
  let startKey = null;
  let scanned = 0;
  let documents = 0;
  let written = 0;
  const pending = [];
  const touched = new Set();

  do {
    const page = JSON.parse(
      aws(
        `dynamodb scan --table-name ${table} --no-paginate ` +
          `--projection-expression "DocumentId, EnrichmentResult" ` +
          (startKey ? `--exclusive-start-key '${JSON.stringify(startKey)}' ` : ""),
        region
      )
    );
    for (const item of page.Items || []) {
      scanned++;
      const raw = item.EnrichmentResult && item.EnrichmentResult.S;
      if (!raw) continue;
      let envelope;
      try {
        envelope = JSON.parse(raw);
      } catch {
        continue;
      }
      const rows = deriveRows(envelope, specsByType, indexedAt);
      if (!rows.length) continue;
      documents++;
      touched.add(rows[0].DocumentId);
      pending.push(...rows);
      for (const row of rows) console.log(`   • ${row.UniqueKey} -> ${row.DocumentId}`);
    }
    startKey = page.LastEvaluatedKey || null;
  } while (startKey);

  if (!dryRun && pending.length) {
    if (opts.prune) {
      const stale = existingRows(keysTable, [...touched], region);
      if (stale.length) {
        batchWrite(keysTable, stale, region, true);
        console.log(`   🧹 ${stale.length} fila(s) previas eliminadas (--prune)`);
      }
    }
    batchWrite(keysTable, pending, region);
    written = pending.length;
  }

  console.log(
    `\n✅ ${scanned} documentos revisados · ${documents} con unique key · ` +
      `${dryRun ? pending.length + " filas (dry-run)" : written + " filas escritas"}`
  );
}

main();
