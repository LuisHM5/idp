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
 *   GET /documents?docType=<tipo>[&field=<dot.path>&value=<v>][&op=eq|contains]
 *                 [&status=<s>][&since=&until=][&order=desc|asc][&limit=N]
 *       Searches processed documents. `docType` accepts the canonical TYPE_ID
 *       (PEDIMENTO_ADUANAL) or its human name/alias ("pedimento aduanal"),
 *       comma-separated for several types, and filters IN DYNAMODB via the
 *       DocumentTypeIndex GSI (PK DocumentType, SK ProcessedAt) — no Scan.
 *       `field`/`value` then filter the matched documents in the Lambda:
 *       `field` is a dot-path resolved against the result envelope (e.g.
 *       extractedEntities.NUMERO_PEDIMENTO_NORMALIZADO); if it does not resolve
 *       there, a deep search matches any nested key equal to the last path
 *       segment. Matching is case-insensitive and trims whitespace.
 *
 * Cost model: with `docType` the query reads only that type's items (Query on
 * the GSI, newest first, server-side filters for status/date/contained type)
 * and BatchGets the envelopes of the survivors. WITHOUT `docType` it falls back
 * to a full-table Scan, because the searchable fields live inside the
 * EnrichmentResult JSON string — pass `docType` whenever you can.
 *
 * The indexed columns (DocumentType, DocumentTypeName, ProcessedAt,
 * ValidationStatus, ContainedDocumentTypes, DocumentName) are written by the
 * enrichment Lambda — see enrichment-lambda/indexing.py. Documents processed
 * before those columns existed are invisible to the GSI until backfilled with
 * `pnpm backfill:index`.
 */

import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import {
  DynamoDBDocumentClient,
  GetCommand,
  BatchGetCommand,
  QueryCommand,
  ScanCommand,
} from "@aws-sdk/lib-dynamodb";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";
import { readFileSync } from "fs";

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}), {
  marshallOptions: { removeUndefinedValues: true },
});
const s3 = new S3Client({});

const TABLE = process.env.DOCUMENT_TABLE;
const BUCKET = process.env.DOCUMENT_BUCKET;
const TYPE_INDEX = process.env.DOCUMENT_TYPE_INDEX || "DocumentTypeIndex";
const CATALOG_TABLE = process.env.CATALOG_TABLE;
const MAX_LIMIT = 100;
/** Types whose items hold several documents inside (see expandContainers). */
const CONTAINER_TYPES = ["EXPEDIENTE_ADUANAL_MULTIDOCUMENTO"];
/** Attributes read from the base table for every candidate. */
const ITEM_PROJECTION = "DocumentId, WorkflowStatus, EnrichmentResult";

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

// ------------------------------------------------------- document types

/**
 * Canonical form of a type name: accent-free, upper snake case.
 * "Pedimento Aduanal" / "pedimento-aduanal" → "PEDIMENTO_ADUANAL".
 */
function canonicalType(raw) {
  return String(raw || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .trim()
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

/**
 * Types bundled at build time (`document-types.json`, generated by
 * scripts/sync-catalogs.mjs from `catalogs/`). Optional: an empty list only
 * means human names cannot be resolved from the bundle.
 */
function bundledTypes() {
  try {
    const file = new URL("./document-types.json", import.meta.url);
    return JSON.parse(readFileSync(file, "utf8")).types || [];
  } catch {
    return [];
  }
}

let knownTypesCache = null;

/**
 * Type ids known to the catalog: the ones bundled at build time
 * (document-types.json, written by scripts/sync-catalogs.mjs) plus whatever the
 * hot-reload DynamoDB catalog holds. Only used to resolve human names to ids,
 * so a failure here is non-fatal — the input is then used as-is.
 */
async function knownTypes() {
  if (knownTypesCache) return knownTypesCache;
  const types = new Map(); // TYPE_ID -> { typeId, names: [canonical names] }

  const add = (typeId, names = []) => {
    const id = canonicalType(typeId);
    if (!id || id.startsWith("_")) return;
    const entry = types.get(id) || { typeId: id, names: [id] };
    for (const n of names) {
      const c = canonicalType(n);
      if (c && !entry.names.includes(c)) entry.names.push(c);
    }
    types.set(id, entry);
  };

  for (const t of bundledTypes()) add(t.typeId, [t.displayName, ...(t.aliases || [])]);

  if (CATALOG_TABLE) {
    try {
      let ExclusiveStartKey;
      do {
        const page = await ddb.send(
          new ScanCommand({
            TableName: CATALOG_TABLE,
            ProjectionExpression: "type_id, display_name, aliases",
            ExclusiveStartKey,
          })
        );
        for (const row of page.Items || []) {
          add(row.type_id, [row.display_name, ...(row.aliases || [])]);
        }
        ExclusiveStartKey = page.LastEvaluatedKey;
      } while (ExclusiveStartKey);
    } catch (err) {
      console.warn("catalog lookup failed, resolving types by name only:", err.message);
    }
  }

  knownTypesCache = [...types.values()];
  return knownTypesCache;
}

/**
 * Resolve one user-supplied `docType` to a canonical TYPE_ID.
 *
 * Order: exact id → exact display name/alias → unique prefix → unique
 * substring. Unresolved input is used canonicalized, so a valid id that is not
 * in the catalog still works (it just returns whatever the index holds).
 */
async function resolveType(raw) {
  const wanted = canonicalType(raw);
  if (!wanted) return null;
  const types = await knownTypes();

  const exact = types.find((t) => t.names.includes(wanted));
  if (exact) return { typeId: exact.typeId, input: raw, resolvedBy: "exact" };

  const prefix = types.filter((t) => t.names.some((n) => n.startsWith(wanted)));
  if (prefix.length === 1) return { typeId: prefix[0].typeId, input: raw, resolvedBy: "prefix" };

  const partial = types.filter((t) => t.names.some((n) => n.includes(wanted)));
  if (partial.length === 1) return { typeId: partial[0].typeId, input: raw, resolvedBy: "partial" };

  if (prefix.length > 1 || partial.length > 1) {
    const candidates = (prefix.length > 1 ? prefix : partial).map((t) => t.typeId);
    return { typeId: wanted, input: raw, resolvedBy: "ambiguous", candidates };
  }
  return { typeId: wanted, input: raw, resolvedBy: "asIs" };
}

// --------------------------------------------------------------- GET by id

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
    documentType: item.DocumentType,
    documentTypeName: item.DocumentTypeName,
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

/**
 * One GSI Query page-walker. Yields index entries (keys + projected columns)
 * for `typeId`, newest first by default, applying every filter DynamoDB can
 * evaluate itself: validation status, processed-at range and — for container
 * types — the contained document type.
 */
async function* queryByType(typeId, { status, since, until, order, containedType }) {
  const names = { "#t": "DocumentType" };
  const values = { ":t": typeId };
  let keyExpr = "#t = :t";
  if (since && until) {
    names["#p"] = "ProcessedAt";
    values[":since"] = since;
    values[":until"] = until;
    keyExpr += " AND #p BETWEEN :since AND :until";
  } else if (since) {
    names["#p"] = "ProcessedAt";
    values[":since"] = since;
    keyExpr += " AND #p >= :since";
  } else if (until) {
    names["#p"] = "ProcessedAt";
    values[":until"] = until;
    keyExpr += " AND #p <= :until";
  }

  const filters = [];
  if (status) {
    names["#s"] = "ValidationStatus";
    values[":s"] = status;
    filters.push("#s = :s");
  }
  if (containedType) {
    names["#c"] = "ContainedDocumentTypes";
    values[":c"] = containedType;
    filters.push("contains(#c, :c)");
  }

  let ExclusiveStartKey;
  do {
    const page = await ddb.send(
      new QueryCommand({
        TableName: TABLE,
        IndexName: TYPE_INDEX,
        KeyConditionExpression: keyExpr,
        ...(filters.length && { FilterExpression: filters.join(" AND ") }),
        ExpressionAttributeNames: names,
        ExpressionAttributeValues: values,
        ScanIndexForward: order === "asc",
        ExclusiveStartKey,
      })
    );
    for (const entry of page.Items || []) yield entry;
    ExclusiveStartKey = page.LastEvaluatedKey;
  } while (ExclusiveStartKey);
}

/**
 * Index entries for the requested types, deduplicated by document id.
 *
 * A multi-document expediente is stored under its container type
 * (EXPEDIENTE_ADUANAL_MULTIDOCUMENTO), so asking for FACTURA must also reach
 * the facturas inside those expedientes: for each requested type we run a
 * second Query per container type filtered by `contains(ContainedDocumentTypes,
 * <type>)`. Still indexed, still no Scan. Disable with `expand=0`.
 */
async function* typeIndexEntries(typeIds, opts) {
  const seen = new Set();
  const plans = [];
  for (const typeId of typeIds) {
    plans.push({ typeId, containedType: null });
    if (opts.expand) {
      for (const container of CONTAINER_TYPES) {
        if (container !== typeId && !typeIds.includes(container)) {
          plans.push({ typeId: container, containedType: typeId });
        }
      }
    }
  }
  for (const plan of plans) {
    for await (const entry of queryByType(plan.typeId, { ...opts, containedType: plan.containedType })) {
      if (seen.has(entry.DocumentId)) continue;
      seen.add(entry.DocumentId);
      yield { ...entry, matchedVia: plan.containedType ? plan.typeId : "type" };
    }
  }
}

/** Fetch the base-table items (with EnrichmentResult) for a batch of ids. */
async function fetchItems(documentIds) {
  if (!documentIds.length) return [];
  const res = await ddb.send(
    new BatchGetCommand({
      RequestItems: {
        [TABLE]: {
          Keys: documentIds.map((DocumentId) => ({ DocumentId })),
          ProjectionExpression: ITEM_PROJECTION,
        },
      },
    })
  );
  const items = (res.Responses && res.Responses[TABLE]) || [];
  // Unprocessed keys (throttling): retry once, sequentially.
  const pending = ((res.UnprocessedKeys || {})[TABLE] || {}).Keys || [];
  if (pending.length) {
    const retry = await ddb.send(
      new BatchGetCommand({ RequestItems: { [TABLE]: { Keys: pending, ProjectionExpression: ITEM_PROJECTION } } })
    );
    items.push(...(((retry.Responses || {})[TABLE]) || []));
  }
  const byId = new Map(items.map((i) => [i.DocumentId, i]));
  return documentIds.map((id) => byId.get(id)).filter(Boolean);
}

async function query(params) {
  const field = params.field || params.path;
  const value = params.value;
  const op = params.op === "contains" ? "contains" : "eq";
  const limit = Math.min(parseInt(params.limit, 10) || 25, MAX_LIMIT);
  const full = params.full === "1" || params.full === "true";
  const summary = params.summary === "1" || params.summary === "true";
  const expand = params.expand !== "0" && params.expand !== "false";
  const order = params.order === "asc" ? "asc" : "desc";
  const status = params.status ? String(params.status).trim().toUpperCase() : undefined;
  const since = params.since || undefined;
  const until = params.until || undefined;

  // docType / documentType / tipo: TYPE_ID or human name, comma-separated.
  const rawTypes = String(params.docType || params.documentType || params.tipo || "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);

  if (!rawTypes.length && (!field || value == null)) {
    return json(400, {
      error: "MISSING_PARAMS",
      message:
        "Provide 'docType' (type id or name) and/or 'field' + 'value' (dot-path search)",
    });
  }
  if (field && value == null) {
    return json(400, {
      error: "MISSING_PARAMS",
      message: "'field' requires 'value'",
    });
  }

  const resolved = [];
  for (const raw of rawTypes) {
    const r = await resolveType(raw);
    if (r) resolved.push(r);
  }
  const docTypes = [...new Set(resolved.map((r) => r.typeId))];

  const matches = [];
  let scanned = 0;

  /** Filter a batch of base items by field/value and collect the matches. */
  const collect = async (items, indexMeta) => {
    for (const item of items) {
      scanned++;
      const envelope = parseEnrichment(item);
      if (!envelope) continue;
      let matchedValue;
      let matchedAt;
      if (field) {
        const m = matchField(envelope, field, value, op);
        if (!m.matched) continue;
        matchedValue = m.value;
        matchedAt = m.where;
      }
      // Default: the complete DynamoDB envelope (documentClassification,
      // validation, expediente.documents[...], tracking) — same shape stored
      // and published downstream. summary=1 returns only a compact view.
      const base = summary ? summarize(envelope) : { ...envelope };
      const meta = indexMeta ? indexMeta.get(item.DocumentId) : undefined;
      const entry = {
        ...base,
        ...(meta && {
          documentType: meta.DocumentType,
          documentTypeName: meta.DocumentTypeName,
          documentName: meta.DocumentName,
          validationStatus: meta.ValidationStatus,
        }),
        ...(field && { matchedValue, matchedAt }),
      };
      // full=1 additionally embeds the deepest S3 result (per-field fields[]
      // detail + full array contents) — costs one S3 GET per match.
      if (full) entry.fullResult = await fetchFullResult(envelope.documentId);
      matches.push(entry);
      if (matches.length >= limit) return true;
    }
    return false;
  };

  if (docTypes.length) {
    // Indexed path: only this type's items are read, in ProcessedAt order.
    const BATCH = 25;
    let buffer = [];
    const indexMeta = new Map();
    const flush = async () => {
      const items = await fetchItems(buffer.map((e) => e.DocumentId));
      buffer = [];
      return collect(items, indexMeta);
    };
    let done = false;
    for await (const entry of typeIndexEntries(docTypes, { status, since, until, order, expand })) {
      indexMeta.set(entry.DocumentId, entry);
      buffer.push(entry);
      // Without a field filter every index hit is a match, so never fetch more
      // envelopes than the caller asked for.
      const target = field ? BATCH : Math.min(BATCH, limit - matches.length);
      if (buffer.length >= target) {
        done = await flush();
        if (done) break;
      }
    }
    if (!done && buffer.length) await flush();
    return respond();
  }

  // Fallback: no docType → full-table Scan (the searchable fields live inside
  // the EnrichmentResult JSON string).
  let ExclusiveStartKey;
  do {
    const page = await ddb.send(
      new ScanCommand({
        TableName: TABLE,
        ProjectionExpression: ITEM_PROJECTION,
        ExclusiveStartKey,
      })
    );
    if (await collect(page.Items || [], null)) return respond();
    ExclusiveStartKey = page.LastEvaluatedKey;
  } while (ExclusiveStartKey);

  return respond();

  function respond() {
    return json(200, {
      ...(docTypes.length && {
        docTypes,
        resolvedTypes: resolved,
        expandedContainers: expand,
      }),
      ...(field && { field, value, op }),
      ...(status && { status }),
      ...(since && { since }),
      ...(until && { until }),
      order,
      summary,
      full,
      indexed: docTypes.length > 0,
      count: matches.length,
      scanned,
      matches,
    });
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
