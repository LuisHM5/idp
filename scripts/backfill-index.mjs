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

/** Same derivation as enrichment-lambda/indexing.py, from a stored envelope. */
function deriveAttributes(envelope) {
  const type = String(envelope.documentClassification || "OTRO").trim().toUpperCase();
  const expediente = envelope.expediente || {};
  const contained = new Set([type]);
  for (const doc of expediente.documents || []) {
    const t = String(doc.documentType || "").trim().toUpperCase();
    if (t) contained.add(t);
  }
  const processedAt =
    envelope.processedAt || expediente.processedAt || new Date(0).toISOString();
  const name = String(expediente.sourceFile || "").split("/").pop();

  const attrs = {
    DocumentType: { S: type },
    DocumentTypeName: { S: NAMES.get(type) || titleize(type) },
    ProcessedAt: { S: String(processedAt) },
    ValidationStatus: { S: String((envelope.validation || {}).status || "REQUIERE_REVISION") },
    ContainedDocumentTypes: { SS: [...contained] },
    IndexedAt: { S: new Date().toISOString() },
  };
  if (name) attrs.DocumentName = { S: name };
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
