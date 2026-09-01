/**
 * QueryLambda - Read/search processed documents.
 *
 * Four endpoints (public like the upload API unless the stage
 * requires an API key — see AMBIENTES_Y_SEGURIDAD.md):
 *
 *   GET /documents/{id}
 *       Returns the full result for a document id. Reads the complete result
 *       from S3 (results/{id}.json) when available, else the compact envelope
 *       stored in DynamoDB. The id may be a prefix — the first match wins.
 *
 *   GET /documents/{id}/file
 *       Returns a short-lived presigned S3 GET URL for the ORIGINAL uploaded
 *       file (the PDF/image/XML/XLSX itself, not the extraction result).
 *       Bytes never pass through this Lambda or API Gateway — see
 *       `findSourceObject`/`getFileById` below and
 *       documentacion/API_USO.md for why (size limits) and the access-control
 *       implications of a presigned URL / a single shared API key.
 *
 *   PATCH /documents/{id}/metadata
 *       Applies a flat JSON Merge Patch to the mutable metadata stored on the
 *       DynamoDB item. It never changes validation status or the immutable
 *       result in S3. Registered apps need AllowMetadataWrite and remain
 *       bounded by AccessScope/AllowedDocTypes.
 *
 *   GET /documents?docType=<tipo>[&system=<app>][&meta.<clave>=<valor>]
 *                 [&field=<dot.path>&value=<v>][&op=eq|contains]
 *                 [&status=<s>][&since=&until=][&order=desc|asc][&limit=N]
 *       Searches processed documents. Every filter below is ANDed, so one
 *       request can ask for "this type, from this system, with this upload
 *       metadata, and this extracted value":
 *
 *         docType   the canonical TYPE_ID (PEDIMENTO_ADUANAL) or its human
 *                   name/alias ("pedimento aduanal"), comma-separated for
 *                   several types. Filtered IN DYNAMODB via the
 *                   DocumentTypeIndex GSI (PK DocumentType, SK ProcessedAt) —
 *                   no Scan. Everything else is applied in this Lambda, over
 *                   the envelopes that survive it.
 *         system    which app uploaded it: `tracking.appOrigin`, resolved from
 *                   the uploader's x-api-key (see the SQS consumer patch).
 *                   Several are ORed. Aliases: `appOrigin`, `sistema`.
 *         meta.*    the caller's own `metadata` from POST /documents, carried
 *                   on the envelope as `metadata` (see handler.py's
 *                   _upload_metadata). Keys are lowercase; repeating one key
 *                   ORs its values, different keys are ANDed. `metaOp=contains`
 *                   switches the whole group to substring matching.
 *         field/value  a dot-path resolved against the result envelope (e.g.
 *                   extractedEntities.NUMERO_PEDIMENTO_NORMALIZADO); if it
 *                   does not resolve there, a deep search matches any nested
 *                   key equal to the last path segment. May be repeated —
 *                   `?field=A&value=1&field=B&value=2` — and the pairs are
 *                   matched by position (see buildFilters/paramList).
 *
 *       Matching is case-insensitive and trims whitespace throughout.
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
 *
 * Authorization model — per-app, resolved from the caller's own x-api-key
 * (`event.requestContext.identity.apiKeyId`, via `resolveCallerApp` below),
 * looked up in the same `document-validation-apps[-dev]` table upload-lambda
 * uses to resolve `appOrigin` (see `manage-apps.mjs` for how these fields
 * are set — `apps:register`/`apps:reconfigure --access-scope/--allowed-doctypes
 * /--allow-field-search/--allow-metadata-write`):
 *
 *   - `AccessScope: "own"` (default for any app registered from here on) —
 *     can only read documents whose `tracking.appOrigin` (see handler.py)
 *     equals the caller's own AppName. `"all"` — no restriction.
 *   - `AllowedDocTypes` — if set, further narrows even an "all"-scope app to
 *     only those types; a `GET /documents?docType=` for anything else is
 *     rejected, and an unscoped search/list is transparently narrowed to just
 *     these types instead of touching the rest of the table.
 *   - `AllowFieldSearch` — whether this app may use `field=`/`value=` at all
 *     (the Scan-based deep search, the most expensive and most abusable
 *     route here) — default false; most apps only need `docType=`/`{id}`.
 *     It does NOT gate `system=`/`meta.<clave>=`: those read one shallow,
 *     known key off the envelope instead of walking it, and they are what an
 *     app with plain docType access needs to find its own document again.
 *   - `AllowMetadataWrite` — whether PATCH may change the mutable Metadata
 *     column. It defaults false for registered apps. AccessScope and
 *     AllowedDocTypes still decide which concrete documents it may change.
 *
 * A denied `GET /documents/{id}` or `/file` returns **404**, not 403 — it
 * must not confirm that a document you can't see even exists. `query()`
 * (list/search) silently drops out-of-scope items from `matches` rather than
 * erroring, same principle.
 *
 * Two callers get NO restriction (`accessScope: "all"`, see `UNRESTRICTED`
 * below), preserving today's behavior for them exactly: no `x-api-key` at all
 * (dev without `DEV_API_KEY_REQUIRED`) and a valid AWS key API Gateway itself
 * accepted but that was never registered here (e.g. `idp-default` before
 * `pnpm apps:adopt` — the pre-existing system-wide key, a deliberate
 * grandfather case, not a bug). Unregistered keys and permission-lookup
 * failures stay fail-open for reads but fail closed for metadata writes — this guards data
 * segregation between already-authenticated apps, not a secret, so an
 * outage in the Apps table should not become an outage of every read.
 */

import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import {
  DynamoDBDocumentClient,
  GetCommand,
  BatchGetCommand,
  QueryCommand,
  ScanCommand,
  UpdateCommand,
} from "@aws-sdk/lib-dynamodb";
import { S3Client, GetObjectCommand, ListObjectsV2Command } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import { readFileSync } from "fs";

import {
  buildFilters,
  buildMetaFilters,
  buildSystems,
  documentAppOrigin,
  matchesMeta,
  matchesSystems,
  matchField,
  paramList,
  splitList,
} from "./search-filters.mjs";
import { parseMetadataPatch } from "./metadata-rules.mjs";

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}), {
  marshallOptions: { removeUndefinedValues: true },
});
const s3 = new S3Client({});

const TABLE = process.env.DOCUMENT_TABLE;
const BUCKET = process.env.DOCUMENT_BUCKET;
const TYPE_INDEX = process.env.DOCUMENT_TYPE_INDEX || "DocumentTypeIndex";
const UNIQUE_KEY_TABLE = process.env.UNIQUE_KEY_TABLE;
const CATALOG_TABLE = process.env.CATALOG_TABLE;
const APPS_TABLE = process.env.APPS_TABLE;
const MAX_LIMIT = 100;
// The blueprint moves the raw upload to processed/{id}/{filename} on any
// execution that completes without an exception (including a RECHAZADO
// validation outcome — that's a business result, not an infra failure), or to
// failed/{id}/{filename} only when the execution itself threw/timed out (see
// AGENTS.md's architecture note and patches/default-sqs-consumer-index.py).
// GET /documents/{id} only ever returns 200 for a document that has already
// been indexed, i.e. already moved out of raw/ — so these two prefixes are
// exhaustive for this endpoint; raw/ is deliberately not searched.
const SOURCE_PREFIXES = [
  { location: "processed", prefix: "processed/" },
  { location: "failed", prefix: "failed/" },
];
const DEFAULT_DOWNLOAD_EXPIRY_SECONDS = 15 * 60;
const MAX_DOWNLOAD_EXPIRY_SECONDS = 60 * 60;
/** Types whose items hold several documents inside (see expandContainers). */
const CONTAINER_TYPES = ["EXPEDIENTE_ADUANAL_MULTIDOCUMENTO"];
/** Attributes read from the base table for every candidate.
 * `Metadata` is aliased because it is close enough to DynamoDB's reserved
 * words not to be worth the gamble, and it is projected on every read because
 * it — not the snapshot inside EnrichmentResult — is the authoritative
 * metadata once an app has patched it (see applyCurrentMetadata). */
const ITEM_PROJECTION = "DocumentId, WorkflowStatus, EnrichmentResult, #md";
const ITEM_PROJECTION_NAMES = { "#md": "Metadata" };

function json(statusCode, payload) {
  return {
    statusCode,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  };
}

/** Parse the rich result stored as a JSON string in the DynamoDB item. */
function isMetadataObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Overlay the mutable DynamoDB Metadata column onto an immutable result
 * snapshot. EnrichmentResult and results/{id}.json record processing-time
 * state; after PATCH, Metadata is authoritative for every API read. */
function applyCurrentMetadata(envelope, item) {
  if (!envelope || !Object.prototype.hasOwnProperty.call(item || {}, "Metadata")) return envelope;
  return { ...envelope, metadata: isMetadataObject(item.Metadata) ? { ...item.Metadata } : {} };
}

function parseEnrichment(item) {
  if (!item || typeof item.EnrichmentResult !== "string") return null;
  try {
    return applyCurrentMetadata(JSON.parse(item.EnrichmentResult), item);
  } catch {
    return null;
  }
}

// ---------------------------------------------------- per-app authorization

/** No x-api-key, or a valid key never registered in APPS_TABLE — today's
 * behavior, unchanged: unrestricted read access. See the module docstring. */
const UNRESTRICTED = {
  appName: null,
  accessScope: "all",
  allowedDocTypes: [],
  allowFieldSearch: true,
  allowMetadataWrite: true,
};

/** Grandfathered read access is not authority to mutate stored data. */
const UNREGISTERED = { ...UNRESTRICTED, allowMetadataWrite: false };

/**
 * Resolve the caller's own app + permissions from its x-api-key, mirroring
 * upload-lambda's `resolveAppOrigin` GetItem-by-ApiKeyId lookup — same table,
 * same hot-path-friendly access pattern (direct GetItem, no Scan).
 */
async function resolveCallerApp(event) {
  const apiKeyId = event.requestContext && event.requestContext.identity && event.requestContext.identity.apiKeyId;
  if (!apiKeyId) return UNRESTRICTED;
  if (!APPS_TABLE) return UNREGISTERED;
  try {
    const { Item } = await ddb.send(
      new GetCommand({ TableName: APPS_TABLE, Key: { ApiKeyId: apiKeyId } })
    );
    if (!Item) return UNREGISTERED; // valid key, never registered — grandfathered reads only
    return {
      appName: Item.AppName,
      accessScope: Item.AccessScope || "own",
      allowedDocTypes: Item.AllowedDocTypes || [],
      allowFieldSearch: !!Item.AllowFieldSearch,
      allowMetadataWrite: !!Item.AllowMetadataWrite,
    };
  } catch (err) {
    console.error("Failed to resolve caller app permissions (failing open — read access unaffected):", err);
    return UNREGISTERED;
  }
}

/** Whether `caller` is allowed to see this specific document. */
function canSeeDocument(caller, envelope) {
  if (!envelope) return false;
  if (caller.allowedDocTypes.length && !caller.allowedDocTypes.includes(envelope.documentClassification)) {
    return false;
  }
  if (caller.accessScope === "own" && documentAppOrigin(envelope) !== caller.appName) {
    return false;
  }
  return true;
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
    // The two filterable axes that are NOT extracted from the document:
    // which app uploaded it and the caller's own upload metadata. Included
    // even when they are the thing being filtered on, so a `summary=1`
    // response can be read back without a second call to confirm what matched.
    ...(documentAppOrigin(envelope) && { appOrigin: documentAppOrigin(envelope) }),
    ...(envelope.metadata && { metadata: envelope.metadata }),
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
 * Accept every shape a unique-key declaration can arrive in — the derived
 * `[{field, normalize}]` that `sync-catalogs.mjs`/`sync-schema.mjs` emit, and
 * a bare field name, so a hand-written DynamoDB catalog row stays valid.
 * The default normalizer must match `unique_keys.py`'s.
 */
function normalizeUniqueKeySpecs(raw) {
  const list = Array.isArray(raw) ? raw : raw ? [raw] : [];
  const specs = [];
  for (const item of list) {
    const field = typeof item === "string" ? item : item && item.field;
    if (!field) continue;
    const normalize = (typeof item === "object" && item.normalize) || "upper";
    specs.push({ field: String(field).trim(), normalize: String(normalize) });
  }
  return specs;
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
  // TYPE_ID -> { typeId, names: [canonical names], uniqueKeys: [{field, normalize}] }
  const types = new Map();

  const add = (typeId, names = [], uniqueKeys = []) => {
    const id = canonicalType(typeId);
    if (!id || id.startsWith("_")) return;
    const entry = types.get(id) || { typeId: id, names: [id], uniqueKeys: [] };
    for (const n of names) {
      const c = canonicalType(n);
      if (c && !entry.names.includes(c)) entry.names.push(c);
    }
    // The DynamoDB catalog is the hot-reload override, and it is added last —
    // so a type whose unique keys changed there wins over the bundled copy
    // instead of merging with a stale declaration.
    if (uniqueKeys.length) entry.uniqueKeys = normalizeUniqueKeySpecs(uniqueKeys);
    types.set(id, entry);
  };

  for (const t of bundledTypes()) {
    add(t.typeId, [t.displayName, ...(t.aliases || [])], t.uniqueKeys || []);
  }

  if (CATALOG_TABLE) {
    try {
      let ExclusiveStartKey;
      do {
        const page = await ddb.send(
          new ScanCommand({
            TableName: CATALOG_TABLE,
            // `unique_keys` is written by scripts/sync-schema.mjs as a
            // derived attribute (the raw `fields` map would be far too big to
            // project here just to read one flag per field). Aliased because
            // it is close enough to DynamoDB's reserved words to not be worth
            // the gamble.
            ProjectionExpression: "type_id, display_name, aliases, #uk",
            ExpressionAttributeNames: { "#uk": "unique_keys" },
            ExclusiveStartKey,
          })
        );
        for (const row of page.Items || []) {
          add(row.type_id, [row.display_name, ...(row.aliases || [])], row.unique_keys || []);
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

/**
 * Resolve `id` (exact or prefix) to its DynamoDB item, shared by every
 * single-document route. Exact id first; else scan (paginating) for the
 * first id that starts with `id`.
 * NOTE: do NOT use Limit on the fallback Scan — on a filtered Scan, Limit
 * caps items EXAMINED per page, not matches, so a matching item that is not
 * first would be missed. Page through until a match is found.
 */
async function resolveItemById(id) {
  const exact = await ddb.send(
    new GetCommand({ TableName: TABLE, Key: { DocumentId: id } })
  );
  if (exact.Item) return exact.Item;

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
    const item = (scan.Items || [])[0];
    if (item) return item;
    ExclusiveStartKey = scan.LastEvaluatedKey;
  } while (ExclusiveStartKey);
  return null;
}

async function getById(id, params, caller) {
  const full = params.full === "1" || params.full === "true";

  const item = await resolveItemById(id);
  if (!item) return json(404, { error: "NOT_FOUND", message: `No document for id '${id}'` });

  const envelope = parseEnrichment(item);
  // 404, not 403: a document outside the caller's scope must not even be
  // confirmed to exist (see the module docstring's authorization model).
  if (!canSeeDocument(caller, envelope)) {
    return json(404, { error: "NOT_FOUND", message: `No document for id '${id}'` });
  }

  const documentId = item.DocumentId;
  // Default result is the DynamoDB envelope (documentClassification, validation,
  // expediente.documents[...], tracking) — the same shape published downstream.
  const resp = {
    documentId,
    workflowStatus: item.WorkflowStatus,
    documentType: item.DocumentType,
    documentTypeName: item.DocumentTypeName,
    result: envelope,
  };
  // full=1 adds the deepest S3 result (per-field detail: fields[] with
  // extractedValue/normalizedValue/status/checks, full array contents).
  if (full) resp.fullResult = await fetchFullResult(documentId, item);
  return json(200, resp);
}

/** Load the complete result from S3 (results/{id}.json), or null. */
async function fetchFullResult(documentId, item) {
  if (!BUCKET) return null;
  try {
    const obj = await s3.send(
      new GetObjectCommand({ Bucket: BUCKET, Key: `results/${documentId}.json` })
    );
    return applyCurrentMetadata(JSON.parse(await obj.Body.transformToString()), item);
  } catch {
    return null;
  }
}

// ---------------------------------------------------- PATCH metadata by id

function parsePatchBody(event) {
  const raw = event.body;
  if (raw === undefined || raw === null || raw === "") return { body: undefined };
  if (typeof raw !== "string") return { body: raw };
  const decoded = event.isBase64Encoded ? Buffer.from(raw, "base64").toString("utf8") : raw;
  try {
    return { body: JSON.parse(decoded) };
  } catch {
    return { error: { code: "INVALID_JSON", message: "Request body is not valid JSON" } };
  }
}

/** Apply one JSON Merge Patch without losing concurrent changes. */
async function patchMetadataById(id, event, caller) {
  if (!caller.allowMetadataWrite) {
    return json(403, {
      error: "FORBIDDEN",
      message: "This app is not allowed to modify metadata. Ask for --allow-metadata-write if it's needed.",
    });
  }

  const parsedBody = parsePatchBody(event);
  if (parsedBody.error) return json(400, { error: parsedBody.error.code, message: parsedBody.error.message });

  let item = await resolveItemById(id);
  if (!item) return json(404, { error: "NOT_FOUND", message: `No document for id '${id}'` });
  const documentId = item.DocumentId;

  // MetadataVersion is an optimistic lock. A competing patch advances it;
  // this request then rereads, reapplies its own merge, and retries.
  for (let attempt = 0; attempt < 4; attempt++) {
    const envelope = parseEnrichment(item);
    if (!canSeeDocument(caller, envelope)) {
      return json(404, { error: "NOT_FOUND", message: `No document for id '${id}'` });
    }

    const current = isMetadataObject(item.Metadata)
      ? item.Metadata
      : isMetadataObject(envelope && envelope.metadata)
        ? envelope.metadata
        : {};
    const patch = parseMetadataPatch(parsedBody.body, current);
    if (patch.error) return json(400, { error: patch.error.code, message: patch.error.message });

    const hasVersion = Number.isFinite(item.MetadataVersion);
    const updatedAt = new Date().toISOString();
    const updatedBy = caller.appName || "dev-unrestricted";
    const values = {
      ":next": patch.next,
      ":zero": 0,
      ":one": 1,
      ":updatedAt": updatedAt,
      ":updatedBy": updatedBy,
      ...(hasVersion && { ":expectedVersion": item.MetadataVersion }),
    };

    try {
      const updated = await ddb.send(
        new UpdateCommand({
          TableName: TABLE,
          Key: { DocumentId: documentId },
          UpdateExpression:
            "SET #md = :next, #mv = if_not_exists(#mv, :zero) + :one, #mu = :updatedAt, #mb = :updatedBy",
          ConditionExpression: hasVersion
            ? "attribute_exists(#id) AND #mv = :expectedVersion"
            : "attribute_exists(#id) AND attribute_not_exists(#mv)",
          ExpressionAttributeNames: {
            "#id": "DocumentId",
            "#md": "Metadata",
            "#mv": "MetadataVersion",
            "#mu": "MetadataUpdatedAt",
            "#mb": "MetadataUpdatedBy",
          },
          ExpressionAttributeValues: values,
          ReturnValues: "ALL_NEW",
        })
      );
      const attrs = updated.Attributes || {};
      return json(200, {
        documentId,
        metadata: isMetadataObject(attrs.Metadata) ? attrs.Metadata : patch.next,
        metadataVersion: attrs.MetadataVersion ?? (hasVersion ? item.MetadataVersion + 1 : 1),
        updatedAt: attrs.MetadataUpdatedAt || updatedAt,
        updatedBy: attrs.MetadataUpdatedBy || updatedBy,
      });
    } catch (err) {
      if (err && err.name === "ConditionalCheckFailedException") {
        const fresh = await ddb.send(new GetCommand({ TableName: TABLE, Key: { DocumentId: documentId } }));
        if (!fresh.Item) return json(404, { error: "NOT_FOUND", message: `No document for id '${id}'` });
        item = fresh.Item;
        continue;
      }
      throw err;
    }
  }

  return json(409, {
    error: "METADATA_CONFLICT",
    message: "Metadata changed concurrently too many times. Retry the same PATCH.",
  });
}

// ------------------------------------------------------- GET file by id

/**
 * Find the original uploaded object for `documentId` under processed/ (tried
 * first — the common case) then failed/. Each folder is
 * `{prefix}{documentId}/{originalFileName}` and holds exactly that one
 * object, so a 1-key listing is enough to both confirm existence and recover
 * the exact filename without trusting the `DocumentName` DynamoDB column
 * (which is best-effort/derived, not authoritative — see indexing.py).
 */
async function findSourceObject(documentId) {
  for (const { location, prefix } of SOURCE_PREFIXES) {
    const listed = await s3.send(
      new ListObjectsV2Command({
        Bucket: BUCKET,
        Prefix: `${prefix}${documentId}/`,
        MaxKeys: 1,
      })
    );
    const obj = (listed.Contents || [])[0];
    if (obj) return { key: obj.Key, location, size: obj.Size };
  }
  return null;
}

/**
 * GET /documents/{id}/file — presigned S3 GET URL for the original file.
 * Bytes never pass through Lambda/API Gateway (see the module docstring for
 * why and its access-control implications): a REST API response is capped at
 * 10MB by API Gateway regardless of Lambda's own limits, and expediente PDFs
 * in this system run into the tens of MB, so proxying was never viable —
 * this mirrors the presigned-PUT path upload-lambda already uses for large
 * uploads (`handlePresigned`).
 */
async function getFileById(id, params, caller) {
  if (!BUCKET) return json(500, { error: "CONFIG_ERROR", message: "DOCUMENT_BUCKET not configured" });

  const item = await resolveItemById(id);
  if (!item) return json(404, { error: "NOT_FOUND", message: `No document for id '${id}'` });
  // Same 404-not-403 rule as getById — this is the highest-stakes check in
  // the file, it hands out the actual bytes.
  if (!canSeeDocument(caller, parseEnrichment(item))) {
    return json(404, { error: "NOT_FOUND", message: `No document for id '${id}'` });
  }
  const documentId = item.DocumentId;

  const source = await findSourceObject(documentId);
  if (!source) {
    return json(404, {
      error: "FILE_NOT_FOUND",
      message: `No original file found for document '${documentId}' (it may still be processing, or the source object was removed)`,
    });
  }

  const requested = parseInt(params.expiresIn, 10);
  const expiresIn = Number.isFinite(requested)
    ? Math.min(Math.max(requested, 60), MAX_DOWNLOAD_EXPIRY_SECONDS)
    : DEFAULT_DOWNLOAD_EXPIRY_SECONDS;

  let downloadUrl;
  try {
    downloadUrl = await getSignedUrl(
      s3,
      new GetObjectCommand({ Bucket: BUCKET, Key: source.key }),
      { expiresIn }
    );
  } catch (err) {
    console.error("Failed to generate presigned download URL:", err);
    return json(500, { error: "PRESIGN_FAILED", message: "Failed to generate download URL" });
  }

  return json(200, {
    documentId,
    fileName: source.key.split("/").pop(),
    fileSizeBytes: source.size,
    location: source.location, // "processed" | "failed"
    downloadUrl,
    downloadMethod: "GET",
    expiresInSeconds: expiresIn,
    expiresAt: new Date(Date.now() + expiresIn * 1000).toISOString(),
  });
}

// --------------------------------------------------------- unique keys

/**
 * Deterministic normalization of a value before it becomes a unique key.
 *
 * MUST stay character-for-character identical to
 * `enrichment-lambda/unique_keys.py::normalize_value` — that module writes the
 * keys, this one reads them, and a disagreement makes every lookup miss
 * silently (it degrades to the slow path, so it fails soft, but it also
 * silently gives up the whole point). Change one, change the other.
 */
function normalizeKeyValue(value, mode = "upper") {
  const text = String(value == null ? "" : value).trim().replace(/\s+/g, " ");
  if (!text) return "";
  if (mode === "raw") return text;
  const upper = text.toUpperCase();
  if (mode === "alnum") return upper.replace(/[^A-Z0-9]/g, "");
  if (mode === "digits") return upper.replace(/[^0-9]/g, "");
  return upper;
}

/** `TIPODOC#VALOR_NORMALIZADO`, or "" when there is nothing to key on. */
function buildUniqueKey(typeId, value, mode) {
  const normalized = normalizeKeyValue(value, mode);
  const type = String(typeId || "").trim().toUpperCase();
  return normalized && type ? `${type}#${normalized}` : "";
}

/**
 * The unique-key specs a type declares that could answer a search on `field`.
 *
 * `field` may be a dot-path (`extractedEntities.NUMERO_PEDIMENTO`), so the
 * last segment is what gets compared — same leniency `matchField` already
 * applies on the slow path.
 */
function uniqueKeySpecsFor(typeEntry, field) {
  const specs = (typeEntry && typeEntry.uniqueKeys) || [];
  if (!specs.length || !field) return [];
  const leaf = String(field).split(".").pop().trim().toUpperCase();
  return specs.filter((spec) => String(spec.field || "").toUpperCase() === leaf);
}

/**
 * Resolve `field`=`value` through the unique-key lookup table.
 *
 * Returns `null` when the fast path does not apply at all (no table, no type
 * declares this field as a unique key) so the caller can take the normal
 * indexed path, or a — possibly empty — list of hits when it does. An empty
 * list is NOT treated as "no such document": the caller still falls back,
 * because a document processed before the key was declared (or before the
 * table existed) has no row yet and must remain findable. See
 * `pnpm backfill:keys`.
 */
async function uniqueKeyLookup(docTypes, field, value, { status, since, until }) {
  if (!UNIQUE_KEY_TABLE || !field || value == null) return null;
  const types = await knownTypes();
  const plans = [];
  for (const typeId of docTypes) {
    const entry = types.find((t) => t.typeId === typeId);
    for (const spec of uniqueKeySpecsFor(entry, field)) {
      const key = buildUniqueKey(typeId, value, spec.normalize);
      if (key) plans.push({ typeId, key, spec });
    }
  }
  if (!plans.length) return null;

  const hits = [];
  const seen = new Set();
  for (const plan of plans) {
    const names = { "#k": "UniqueKey" };
    const values = { ":k": plan.key };
    const filters = [];
    if (status) {
      names["#s"] = "ValidationStatus";
      values[":s"] = status;
      filters.push("#s = :s");
    }
    if (since) {
      names["#p"] = "ProcessedAt";
      values[":since"] = since;
      filters.push("#p >= :since");
    }
    if (until) {
      names["#p"] = "ProcessedAt";
      values[":until"] = until;
      filters.push("#p <= :until");
    }
    let ExclusiveStartKey;
    do {
      const page = await ddb.send(
        new QueryCommand({
          TableName: UNIQUE_KEY_TABLE,
          KeyConditionExpression: "#k = :k",
          ...(filters.length && { FilterExpression: filters.join(" AND ") }),
          ExpressionAttributeNames: names,
          ExpressionAttributeValues: values,
          ExclusiveStartKey,
        })
      );
      for (const row of page.Items || []) {
        if (seen.has(row.DocumentId)) continue;
        seen.add(row.DocumentId);
        hits.push({ ...row, matchedKey: plan.key });
      }
      ExclusiveStartKey = page.LastEvaluatedKey;
    } while (ExclusiveStartKey);
  }
  return hits;
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
          ExpressionAttributeNames: ITEM_PROJECTION_NAMES,
        },
      },
    })
  );
  const items = (res.Responses && res.Responses[TABLE]) || [];
  // Unprocessed keys (throttling): retry once, sequentially.
  const pending = ((res.UnprocessedKeys || {})[TABLE] || {}).Keys || [];
  if (pending.length) {
    const retry = await ddb.send(
      new BatchGetCommand({
        RequestItems: {
          [TABLE]: {
            Keys: pending,
            ProjectionExpression: ITEM_PROJECTION,
            ExpressionAttributeNames: ITEM_PROJECTION_NAMES,
          },
        },
      })
    );
    items.push(...(((retry.Responses || {})[TABLE]) || []));
  }
  const byId = new Map(items.map((i) => [i.DocumentId, i]));
  return documentIds.map((id) => byId.get(id)).filter(Boolean);
}

async function query(params, caller, multi) {
  const built = buildFilters(params, multi);
  // buildFilters reports `{code, message}` (it is HTTP-agnostic, like
  // body-parser.mjs); the wire shape is `{error, message}` — see ErrorResponse
  // in openapi.yaml, which callers program against.
  if (built.error) return json(400, { error: built.error.code, message: built.error.message });
  const filters = built.filters;
  const metaFilters = buildMetaFilters(params, multi);
  const metaOp = params.metaOp === "contains" ? "contains" : "eq";
  const systems = buildSystems(params, multi);

  const limit = Math.min(parseInt(params.limit, 10) || 25, MAX_LIMIT);
  const full = params.full === "1" || params.full === "true";
  const summary = params.summary === "1" || params.summary === "true";
  const expand = params.expand !== "0" && params.expand !== "false";
  const order = params.order === "asc" ? "asc" : "desc";
  const status = params.status ? String(params.status).trim().toUpperCase() : undefined;
  const since = params.since || undefined;
  const until = params.until || undefined;

  // docType / documentType / tipo: TYPE_ID or human name, comma-separated
  // and/or repeated.
  const rawTypes = [
    ...new Set(
      splitList([
        ...paramList(params, multi, "docType"),
        ...paramList(params, multi, "documentType"),
        ...paramList(params, multi, "tipo"),
      ])
    ),
  ];

  if (!rawTypes.length && !filters.length && !systems.length && !metaFilters.length) {
    return json(400, {
      error: "MISSING_PARAMS",
      message:
        "Provide at least one of: 'docType' (type id or name), 'field' + 'value' " +
        "(dot-path search), 'system' (uploading app) or 'meta.<key>' (upload metadata)",
    });
  }
  // The Scan-based deep search is the most expensive and most abusable route
  // here (see the module docstring) — most apps only need docType/id lookups.
  // `system`/`meta.<key>` are deliberately NOT behind this permission: both
  // read one shallow, known key off the envelope instead of walking it, and
  // they are exactly what an app with plain docType access needs to find its
  // own document again ("the one I uploaded for lote A123").
  if (filters.length && !caller.allowFieldSearch) {
    return json(403, {
      error: "FORBIDDEN",
      message: "This app is not allowed to use field/value search. Ask for --allow-field-search if it's needed.",
    });
  }

  const resolved = [];
  for (const raw of rawTypes) {
    const r = await resolveType(raw);
    if (r) resolved.push(r);
  }
  let docTypes = [...new Set(resolved.map((r) => r.typeId))];

  // AllowedDocTypes narrows (never widens) what this app can see: an
  // unscoped search ("everything") is silently narrowed to just the allowed
  // types instead of touching the rest of the table; an explicit docType
  // outside that list is rejected rather than silently dropped, since the
  // caller asked for it by name.
  if (caller.allowedDocTypes.length) {
    if (!docTypes.length) {
      docTypes = [...caller.allowedDocTypes];
    } else {
      const denied = docTypes.filter((t) => !caller.allowedDocTypes.includes(t));
      if (denied.length) {
        return json(403, {
          error: "FORBIDDEN",
          message: `This app is not allowed to query docType(s): ${denied.join(", ")}`,
        });
      }
    }
  }

  const matches = [];
  let scanned = 0;

  /** Filter a batch of base items and collect the matches.
   *
   * Every filter is ANDed: the uploading system, then the upload metadata
   * (both shallow, known keys — cheapest first), then each field/value pair.
   *
   * `preMatched` (unique-key path) carries the row the lookup table already
   * resolved for a document id, and `driver` says WHICH filter it answered.
   * That one filter bypasses `matchField` on purpose: the key is a NORMALIZED
   * form of the value (`digits` collapses `26 07 0823 6001630` and
   * `260708236001630` onto one key), so re-comparing the raw envelope value
   * against the caller's raw input would drop documents the index legitimately
   * matched. Every OTHER filter still has to hold. */
  const collect = async (items, indexMeta, preMatched, driver) => {
    for (const item of items) {
      scanned++;
      const envelope = parseEnrichment(item);
      if (!envelope) continue;
      if (!canSeeDocument(caller, envelope)) continue;
      if (!matchesSystems(envelope, systems)) continue;
      if (!matchesMeta(envelope, metaFilters, metaOp)) continue;

      const matched = [];
      let rejected = false;
      for (const f of filters) {
        const hit = f === driver && preMatched && preMatched.get(item.DocumentId);
        if (hit) {
          matched.push({
            field: f.field,
            matchedValue: hit.Value,
            matchedAt: `${hit.DocumentType}.${hit.FieldName}`,
          });
          continue;
        }
        const m = matchField(envelope, f.field, f.value, f.op);
        if (!m.matched) {
          rejected = true;
          break;
        }
        matched.push({ field: f.field, matchedValue: m.value, matchedAt: m.where });
      }
      if (rejected) continue;

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
        // `matched` has one entry per field filter, in the order they were
        // given. matchedValue/matchedAt keep naming the FIRST of them, so a
        // single-filter caller sees the exact response shape it saw before
        // several filters were possible.
        ...(matched.length && {
          matched,
          matchedValue: matched[0].matchedValue,
          matchedAt: matched[0].matchedAt,
        }),
      };
      // full=1 additionally embeds the deepest S3 result (per-field fields[]
      // detail + full array contents) — costs one S3 GET per match.
      if (full) entry.fullResult = await fetchFullResult(envelope.documentId, item);
      matches.push(entry);
      if (matches.length >= limit) return true;
    }
    return false;
  };

  // Fast path: one of the filters is on a field the type declares as its
  // unique key, so the answer starts from one Query on `TIPODOC#valor` instead
  // of a walk over every document of the type — the remaining filters then
  // only run against that handful of documents. `op=contains` can't use it (a
  // hash key has no substring semantics) and neither can a search with no
  // docType (the key is always type-scoped — that's what makes it unique).
  let lookupPath = docTypes.length ? "typeIndex" : "scan";
  if (docTypes.length) {
    for (const driver of filters) {
      if (driver.op !== "eq") continue;
      const hits = await uniqueKeyLookup(docTypes, driver.field, driver.value, { status, since, until });
      // hits === null  → no type declares this field as a unique key.
      // hits.length === 0 → nothing indexed under it *yet* (a document
      // processed before the declaration, or before `pnpm backfill:keys` ran).
      // Both fall through to the normal path, which still finds it.
      if (!hits || !hits.length) continue;
      lookupPath = "uniqueKey";
      const byId = new Map(hits.map((h) => [h.DocumentId, h]));
      const ordered = hits
        .sort((a, b) =>
          order === "asc"
            ? String(a.ProcessedAt).localeCompare(String(b.ProcessedAt))
            : String(b.ProcessedAt).localeCompare(String(a.ProcessedAt))
        )
        .map((h) => h.DocumentId);
      // Batched rather than one BatchGet of the first `limit` ids: `collect`
      // also drops documents this app may not see (and now, documents the
      // other filters reject), so slicing up front could return fewer than
      // `limit` while matches were still available. 25 also keeps every
      // request under BatchGetItem's 100-key cap.
      const BATCH = 25;
      for (let i = 0; i < ordered.length; i += BATCH) {
        const done = await collect(await fetchItems(ordered.slice(i, i + BATCH)), null, byId, driver);
        if (done) break;
      }
      return respond();
    }
  }

  // Whether a document that reached the index still has to be checked in the
  // Lambda. Without any of these every index hit is already a match, so the
  // walk can stop at exactly `limit` envelopes.
  const needsPostFilter = filters.length > 0 || systems.length > 0 || metaFilters.length > 0;

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
      const target = needsPostFilter ? BATCH : Math.min(BATCH, limit - matches.length);
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
        ExpressionAttributeNames: ITEM_PROJECTION_NAMES,
        ExclusiveStartKey,
      })
    );
    if (await collect(page.Items || [], null)) return respond();
    ExclusiveStartKey = page.LastEvaluatedKey;
  } while (ExclusiveStartKey);

  return respond();

  function respond() {
    const first = filters[0];
    return json(200, {
      ...(docTypes.length && {
        docTypes,
        resolvedTypes: resolved,
        expandedContainers: expand,
      }),
      // `field`/`value`/`op` echo the FIRST filter, unchanged from when only
      // one was possible; `filters` is the whole list and is what to read
      // when there may be several.
      ...(first && { field: first.field, value: first.value, op: first.op, filters }),
      ...(systems.length && { system: systems }),
      ...(metaFilters.length && { metaFilters, metaOp }),
      ...(status && { status }),
      ...(since && { since }),
      ...(until && { until }),
      order,
      summary,
      full,
      indexed: docTypes.length > 0,
      // Which access path actually answered: "uniqueKey" (one Query on the
      // TIPODOC#valor lookup table), "typeIndex" (Query on DocumentTypeIndex
      // + in-Lambda filtering) or "scan" (no docType — full table Scan).
      lookup: lookupPath,
      // Present whenever the caller's own permissions shaped the results —
      // so "why don't I see X" is answerable from the response itself.
      ...(caller.accessScope === "own" && { restrictedToApp: caller.appName }),
      ...(caller.allowedDocTypes.length && { restrictedToDocTypes: caller.allowedDocTypes }),
      count: matches.length,
      scanned,
      matches,
    });
  }
}

// --------------------------------------------------------------- handler

export async function handler(event) {
  const id = event.pathParameters && event.pathParameters.id;
  const params = event.queryStringParameters || {};
  // Repeated query params (several `field`/`value` pairs) only survive in the
  // multi-value map — see paramList.
  const multi = event.multiValueQueryStringParameters || null;
  // `event.resource` is API Gateway's resource path template
  // ("/documents/{id}/file" vs "/documents/{id}"), stable regardless of how
  // `id` was URL-encoded — safer than pattern-matching `event.path`.
  const isFileRoute = event.resource === "/documents/{id}/file";
  const isMetadataRoute = event.resource === "/documents/{id}/metadata";
  try {
    const caller = await resolveCallerApp(event);
    if (id && isFileRoute) return await getFileById(decodeURIComponent(id), params, caller);
    if (id && isMetadataRoute && event.httpMethod === "PATCH") {
      return await patchMetadataById(decodeURIComponent(id), event, caller);
    }
    if (id) return await getById(decodeURIComponent(id), params, caller);
    return await query(params, caller, multi);
  } catch (err) {
    return json(500, { error: "INTERNAL_ERROR", message: String(err && err.message || err) });
  }
}
