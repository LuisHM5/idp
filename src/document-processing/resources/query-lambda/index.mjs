/**
 * QueryLambda - Read/search processed documents.
 *
 * Two endpoints (both GET, public like the upload API):
 *
 *   GET /documents/{id}
 *       Returns the full result for a document id. Reads the complete result
 *       from S3 (results/{id}.json) when available, else the compact envelope
 *       stored in DynamoDB. The id may be a prefix — the first match wins.
 *
 *   GET /documents?field=<dot.path>&value=<v>[&op=eq|contains][&limit=N]
 *       Scans processed documents and returns those whose `field` matches
 *       `value`. `field` is a dot-path resolved against the result envelope
 *       (e.g. extractedEntities.NUMERO_PEDIMENTO_NORMALIZADO); if it does not
 *       resolve there, a deep search matches any nested key equal to the last
 *       path segment. Matching is case-insensitive and trims whitespace.
 *
 * NOTE: the query path uses a full-table Scan (the searchable data lives inside
 * the EnrichmentResult JSON string, not as indexed attributes). Fine for modest
 * volumes; add a GSI for a specific hot field if the table grows large.
 */

import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import {
  DynamoDBDocumentClient,
  GetCommand,
  ScanCommand,
} from "@aws-sdk/lib-dynamodb";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}), {
  marshallOptions: { removeUndefinedValues: true },
});
const s3 = new S3Client({});

const TABLE = process.env.DOCUMENT_TABLE;
const BUCKET = process.env.DOCUMENT_BUCKET;
const MAX_LIMIT = 100;

function json(statusCode, payload) {
  return {
    statusCode,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  };
}

/** Parse the rich result stored as a JSON string in the DynamoDB item. */
function parseEnrichment(item) {
  if (!item || typeof item.EnrichmentResult !== "string") return null;
  try {
    return JSON.parse(item.EnrichmentResult);
  } catch {
    return null;
  }
}

/** Resolve a dot-path (no array indices) against an object. */
function resolvePath(obj, path) {
  let cur = obj;
  for (const key of path.split(".")) {
    if (cur == null || typeof cur !== "object") return undefined;
    cur = cur[key];
  }
  return cur;
}

function norm(v) {
  return String(v == null ? "" : v).trim().toLowerCase();
}

function compare(actual, expected, op) {
  if (actual == null) return false;
  const a = norm(actual);
  const b = norm(expected);
  return op === "contains" ? a.includes(b) : a === b;
}

/**
 * Deep search: does any key named `leaf` anywhere in `obj` match `value`?
 * Returns the first matching value, or undefined.
 */
function deepFind(obj, leaf, expected, op) {
  const stack = [obj];
  while (stack.length) {
    const cur = stack.pop();
    if (Array.isArray(cur)) {
      for (const el of cur) if (el && typeof el === "object") stack.push(el);
    } else if (cur && typeof cur === "object") {
      for (const [k, v] of Object.entries(cur)) {
        if (k === leaf && compare(v, expected, op)) return v;
        if (v && typeof v === "object") stack.push(v);
      }
    }
  }
  return undefined;
}

/** Try exact dot-path first, then a deep search by the leaf key. */
function matchField(envelope, path, value, op) {
  const direct = resolvePath(envelope, path);
  if (direct !== undefined && typeof direct !== "object") {
    if (compare(direct, value, op)) return { matched: true, value: direct, where: path };
    return { matched: false };
  }
  const leaf = path.split(".").pop();
  const found = deepFind(envelope, leaf, value, op);
  if (found !== undefined) return { matched: true, value: found, where: `*.${leaf}` };
  return { matched: false };
}

/** Compact one document result for list/search responses.
 *
 * Includes the primary document's arrayCounts and liquidacionNormalizada so the
 * summary is useful on its own; the full per-field detail + array contents live
 * in S3 (results/{id}.json) — request them with `full=1` or GET /documents/{id}.
 */
function summarize(envelope) {
  const exp = envelope.expediente || {};
  const primary = (exp.documents || [])[0] || {};
  return {
    documentId: envelope.documentId,
    documentClassification: envelope.documentClassification,
    status: (envelope.validation && envelope.validation.status) || exp.status,
    processedAt: envelope.processedAt,
    extractedEntities: envelope.extractedEntities || {},
    arrayCounts: primary.arrayCounts || {},
    liquidacionNormalizada: primary.liquidacionNormalizada,
  };
}

// -------------------------------------------------------------- GET by id

async function getById(id, params) {
  const full = params.full === "1" || params.full === "true";

  // Exact id first.
  const exact = await ddb.send(
    new GetCommand({ TableName: TABLE, Key: { DocumentId: id } })
  );
  let item = exact.Item;

  // Prefix match: scan (paginating) for the first id that starts with `id`.
  // NOTE: do NOT use Limit here — on a filtered Scan, Limit caps items
  // EXAMINED per page, not matches, so a matching item that is not first
  // would be missed. Page through until a match is found.
  if (!item) {
    let ExclusiveStartKey;
    do {
      const scan = await ddb.send(
        new ScanCommand({
          TableName: TABLE,
          FilterExpression: "begins_with(DocumentId, :p)",
          ExpressionAttributeValues: { ":p": id },
          ExclusiveStartKey,
        })
      );
      item = (scan.Items || [])[0];
      ExclusiveStartKey = scan.LastEvaluatedKey;
    } while (!item && ExclusiveStartKey);
  }
  if (!item) return json(404, { error: "NOT_FOUND", message: `No document for id '${id}'` });

  const documentId = item.DocumentId;
  // Default result is the DynamoDB envelope (documentClassification, validation,
  // expediente.documents[...], tracking) — the same shape published downstream.
  const resp = {
    documentId,
    workflowStatus: item.WorkflowStatus,
    result: parseEnrichment(item),
  };
  // full=1 adds the deepest S3 result (per-field detail: fields[] with
  // extractedValue/normalizedValue/status/checks, full array contents).
  if (full) resp.fullResult = await fetchFullResult(documentId);
  return json(200, resp);
}

/** Load the complete result from S3 (results/{id}.json), or null. */
async function fetchFullResult(documentId) {
  if (!BUCKET) return null;
  try {
    const obj = await s3.send(
      new GetObjectCommand({ Bucket: BUCKET, Key: `results/${documentId}.json` })
    );
    return JSON.parse(await obj.Body.transformToString());
  } catch {
    return null;
  }
}

// --------------------------------------------------------------- query

async function query(params) {
  const field = params.field || params.path;
  const value = params.value;
  const op = params.op === "contains" ? "contains" : "eq";
  const limit = Math.min(parseInt(params.limit, 10) || 25, MAX_LIMIT);
  const full = params.full === "1" || params.full === "true";
  const summary = params.summary === "1" || params.summary === "true";

  if (!field || value == null) {
    return json(400, {
      error: "MISSING_PARAMS",
      message: "Provide 'field' (dot-path) and 'value' query params",
    });
  }

  const matches = [];
  let ExclusiveStartKey;
  let scanned = 0;
  do {
    const page = await ddb.send(
      new ScanCommand({
        TableName: TABLE,
        ProjectionExpression: "DocumentId, WorkflowStatus, EnrichmentResult",
        ExclusiveStartKey,
      })
    );
    for (const item of page.Items || []) {
      scanned++;
      const envelope = parseEnrichment(item);
      if (!envelope) continue;
      const m = matchField(envelope, field, value, op);
      if (m.matched) {
        // Default: the complete DynamoDB envelope (documentClassification,
        // validation, expediente.documents[...], tracking) — same shape stored
        // and published downstream. summary=1 returns only a compact view.
        const base = summary ? summarize(envelope) : { ...envelope };
        const entry = { ...base, matchedValue: m.value, matchedAt: m.where };
        // full=1 additionally embeds the deepest S3 result (per-field fields[]
        // detail + full array contents) — costs one S3 GET per match.
        if (full) entry.fullResult = await fetchFullResult(envelope.documentId);
        matches.push(entry);
        if (matches.length >= limit) return respond();
      }
    }
    ExclusiveStartKey = page.LastEvaluatedKey;
  } while (ExclusiveStartKey);

  return respond();

  function respond() {
    return json(200, { field, value, op, summary, full, count: matches.length, scanned, matches });
  }
}

// --------------------------------------------------------------- handler

export async function handler(event) {
  const id = event.pathParameters && event.pathParameters.id;
  try {
    if (id) return await getById(decodeURIComponent(id), event.queryStringParameters || {});
    return await query(event.queryStringParameters || {});
  } catch (err) {
    return json(500, { error: "INTERNAL_ERROR", message: String(err && err.message || err) });
  }
}
