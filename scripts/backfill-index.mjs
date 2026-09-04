#!/usr/bin/env node
/**
 * Backfill the indexed document-type columns on the processing table.
 *
 * The enrichment Lambda writes DocumentType / DocumentTypeName / ProcessedAt /
 * ValidationStatus / ContainedDocumentTypes / DocumentName on every run (see
 * enrichment-lambda/indexing.py), which is what the DocumentTypeIndex GSI
 * indexes. Documents processed BEFORE that existed only have EnrichmentResult,
 * so they never show up in `GET /documents?docType=...`. This script rebuilds
 * those columns from the stored EnrichmentResult JSON.
 *
 * Usage:
 *   pnpm backfill:index                       # tabla del stack, todos los items
 *   pnpm backfill:index -- --dry-run          # solo muestra lo que escribiría
 *   pnpm backfill:index -- --force            # reescribe también los ya indexados
 *   pnpm backfill:index -- --table NAME --region us-east-1
 *
 * Idempotent: without --force it skips items that already have DocumentType.
 * Uses the AWS CLI (same approach as scripts/sync-schema.mjs) so it needs no
 * extra npm dependency — just credentials with dynamodb:Scan/UpdateItem.
 */

import { readFileSync, writeFileSync, unlinkSync } from "fs";
import { execSync } from "child_process";
import { readDocuments } from "../src/document-processing/resources/query-lambda/envelope-shape.mjs";

const STACK = process.env.STACK_NAME || "DocumentValidationStack";

function parseArgs(argv) {
  const opts = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--" || !a.startsWith("--")) continue;
    const key = a.slice(2);
    if (["dry-run", "force"].includes(key)) opts[key] = true;
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

/** Locate the processing table from the deployed stack when not given. */
function resolveTable(region) {
  const out = aws(
    `cloudformation describe-stack-resources --stack-name ${STACK} ` +
      `--query "StackResources[?ResourceType=='AWS::DynamoDB::Table'].PhysicalResourceId"`,
    region
  );
  const tables = JSON.parse(out).filter((t) => t.includes("DocumentProcessingTable"));
  if (!tables.length) {
    throw new Error(
      `No encontré la tabla de procesamiento en el stack ${STACK}; pásala con --table`
    );
  }
  return tables[0];
}

/** "PEDIMENTO_ADUANAL" -> "Pedimento Aduanal" — mismo criterio que indexing.py. */
function titleize(typeId) {
  return typeId
    .split("_")
    .filter(Boolean)
    .map((w) => w.charAt(0) + w.slice(1).toLowerCase())
    .join(" ");
}

/** Human names declared in the catalog, when the build generated them. */
function displayNames() {
  try {
    const file = new URL(
      "../src/document-processing/resources/query-lambda/document-types.json",
      import.meta.url
    );
    const { types = [] } = JSON.parse(readFileSync(file, "utf8"));
    return new Map(types.map((t) => [t.typeId, t.displayName]));
  } catch {
    return new Map();
  }
}

const NAMES = displayNames();

/** Low-level DynamoDB JSON typed value for a plain JS value (recursive —
 * `Tracking`/`ModelIds` are nested maps). Mirrors what the AWS CLI's
 * `--cli-input-json` expects; boto3's resource API (indexing.py) does this
 * marshalling itself, this script talks to the CLI directly so it can't. */
function toDdbAttr(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return { S: value };
  if (typeof value === "number") return { N: String(value) };
  if (typeof value === "boolean") return { BOOL: value };
  if (Array.isArray(value)) {
    const items = value.map(toDdbAttr).filter(Boolean);
    return items.length ? { L: items } : null;
  }
  if (typeof value === "object") {
    const m = {};
    for (const [k, v] of Object.entries(value)) {
      const attr = toDdbAttr(v);
      if (attr) m[k] = attr;
    }
    return Object.keys(m).length ? { M: m } : null;
  }
  return null;
}

// camelCase (envelope's `tracking`) -> PascalCase (DynamoDB column naming
// convention) — same map as indexing.py::_TRACKING_FIELD_MAP, every field
// build_tracking() sets plus modelIds/appOrigin, so this never lags behind
// what the nested `expediente.tracking` already carries.
const TRACKING_FIELD_MAP = {
  createdAt: "CreatedAt",
  updatedAt: "UpdatedAt",
  processingStartedAt: "ProcessingStartedAt",
  processingCompletedAt: "ProcessingCompletedAt",
  processingSeconds: "ProcessingSeconds",
  processingMs: "ProcessingTime",
  schema: "Schema",
  appOrigin: "AppOrigin",
  modelIds: "ModelIds",
};

/** Same shape as indexing.py::_tracking_attribute, from a stored envelope's
 * top-level `tracking`. */
function trackingAttribute(envelope) {
  const tracking = envelope.tracking || {};
  const out = {};
  for (const [camelKey, pascalKey] of Object.entries(TRACKING_FIELD_MAP)) {
    const value = tracking[camelKey];
    if (value !== undefined && value !== null) out[pascalKey] = value;
  }
  return Object.keys(out).length ? out : null;
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

/** Same derivation as enrichment-lambda/indexing.py, from a stored envelope. */
function deriveAttributes(envelope) {
  const type = indexTypeOf(
    String(envelope.documentClassification || "OTRO").trim().toUpperCase()
  );
  // Relee envelopes de CUALQUIER edad: v2 los trae en la raíz, v1 dentro de
  // `expediente` (ver query-lambda/envelope-shape.mjs).
  const expediente = envelope.expediente || {};
  const contained = new Set([type]);
  for (const doc of readDocuments(envelope)) {
    const t = String(doc.documentType || "").trim().toUpperCase();
    if (t) contained.add(t);
  }
  const processedAt =
    envelope.processedAt || expediente.processedAt || new Date(0).toISOString();
  const name = String(envelope.sourceFile || expediente.sourceFile || "").split("/").pop();

  const attrs = {
    DocumentType: { S: type },
    DocumentTypeName: { S: NAMES.get(type) || titleize(type) },
    ProcessedAt: { S: String(processedAt) },
    ValidationStatus: { S: String((envelope.validation || {}).status || "REQUIERE_REVISION") },
    ContainedDocumentTypes: { SS: [...contained] },
    IndexedAt: { S: new Date().toISOString() },
  };
  if (name) attrs.DocumentName = { S: name };
  const tracking = trackingAttribute(envelope);
  if (tracking) attrs.Tracking = toDdbAttr(tracking);
  // The caller's own upload metadata, same column indexing.py writes. Only
  // documents processed after it started being carried through the pipeline
  // have it in the envelope at all — for the older ones there is nothing to
  // recover here, since it only ever lived on the raw S3 object.
  const metadata = envelope.metadata;
  if (metadata && typeof metadata === "object" && !Array.isArray(metadata)) {
    const attr = toDdbAttr(metadata);
    if (attr) attrs.Metadata = attr;
  }
  return attrs;
}

function updateItem(table, documentId, attrs, region) {
  const names = {};
  const values = {};
  const sets = [];
  Object.entries(attrs).forEach(([key, value], i) => {
    names[`#a${i}`] = key;
    values[`:a${i}`] = value;
    sets.push(`#a${i} = :a${i}`);
  });
  const payload = {
    TableName: table,
    Key: { DocumentId: { S: documentId } },
    UpdateExpression: `SET ${sets.join(", ")}`,
    ExpressionAttributeNames: names,
    ExpressionAttributeValues: values,
  };
  const tmp = `/tmp/ddb-index-${Date.now()}-${Math.random().toString(36).slice(2)}.json`;
  writeFileSync(tmp, JSON.stringify(payload));
  try {
    aws(`dynamodb update-item --cli-input-json file://${tmp}`, region);
  } finally {
    unlinkSync(tmp);
  }
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const region = opts.region || process.env.AWS_REGION || "us-east-1";
  const dryRun = !!opts["dry-run"];
  const force = !!opts.force;
  const table = opts.table || resolveTable(region);

  console.log(`📇 Backfill de columnas indexadas en ${table} (${region})`);
  if (dryRun) console.log("   modo dry-run: no se escribe nada\n");

  let startKey = null;
  let scanned = 0;
  let updated = 0;
  let skipped = 0;
  let failed = 0;

  do {
    // Paginación manual (--no-paginate + LastEvaluatedKey): así controlamos el
    // avance y no dependemos del paginador del CLI.
    const page = JSON.parse(
      aws(
        `dynamodb scan --table-name ${table} --no-paginate ` +
          `--projection-expression "DocumentId, EnrichmentResult, DocumentType" ` +
          (startKey ? `--exclusive-start-key '${JSON.stringify(startKey)}' ` : ""),
        region
      )
    );

    for (const item of page.Items || []) {
      scanned++;
      const documentId = item.DocumentId && item.DocumentId.S;
      if (!documentId) continue;
      if (!force && item.DocumentType && item.DocumentType.S) {
        skipped++;
        continue;
      }
      const raw = item.EnrichmentResult && item.EnrichmentResult.S;
      if (!raw) {
        skipped++;
        continue;
      }
      let envelope;
      try {
        envelope = JSON.parse(raw);
      } catch {
        console.warn(`  ⚠ ${documentId}: EnrichmentResult ilegible`);
        failed++;
        continue;
      }
      const attrs = deriveAttributes(envelope);
      if (dryRun) {
        console.log(
          `  🔎 ${documentId} → ${attrs.DocumentType.S} ` +
            `[${attrs.ContainedDocumentTypes.SS.join(", ")}] ${attrs.ProcessedAt.S}`
        );
        updated++;
        continue;
      }
      try {
        updateItem(table, documentId, attrs, region);
        updated++;
        console.log(`  ✅ ${documentId} → ${attrs.DocumentType.S}`);
      } catch (e) {
        failed++;
        console.error(`  ❌ ${documentId}: ${e.stderr?.toString().trim() || e.message}`);
      }
    }

    startKey = page.LastEvaluatedKey || null;
  } while (startKey);

  console.log(
    `\n📊 revisados ${scanned} · indexados ${updated} · omitidos ${skipped} · errores ${failed}`
  );
  if (failed) process.exitCode = 1;
}

main();
