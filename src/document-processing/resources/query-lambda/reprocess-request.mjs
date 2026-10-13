/**
 * Pure request/target helpers for POST /documents/{id}/reprocess.
 *
 * QueryLambda and UploadLambda are deployed from separate asset directories,
 * so this file intentionally mirrors the documentType grammar in
 * upload-lambda/body-parser.mjs.  The parity is executable, not aspirational:
 * tests/unit/node/reprocess-request.test.mjs runs the same cases through both.
 */

const DOCUMENT_TYPE_SHAPE = /^[A-Z][A-Z0-9_]{0,63}$/;
const TYPE_COUNT_SPLIT = /^([^:]*)(?::(.*))?$/;
const MAX_DOCUMENT_TYPES = 8;
const MAX_DOCUMENT_TYPES_CHARS = 200;

const ALLOWED_EXTENSIONS = new Set(["jpg", "jpeg", "png", "pdf", "xml", "xlsx", "csv"]);
const REQUIRES_DECLARED_TYPE = new Set(["xml", "xlsx", "csv"]);
const SYSTEM_OBJECT_METADATA = new Set([
  "app-origin",
  "model-id",
  "extraction-mode",
  "ai-response-format",
  "document-counts",
]);

export function aiResponseFormatOf(body) {
  const value = body?.aiResponseFormat;
  if (value === undefined || value === null || value === "") return { format: null };
  const format = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (!new Set(["compact", "legacy"]).has(format)) {
    return {
      error: {
        code: "INVALID_AI_RESPONSE_FORMAT",
        message: "Field 'aiResponseFormat' must be 'compact' or 'legacy'",
      },
    };
  }
  return { format };
}

export function parseBody(event) {
  const raw = event.body;
  if (raw === undefined || raw === null || raw === "") return { body: {} };
  if (typeof raw !== "string") return { body: raw };

  const decoded = event.isBase64Encoded ? Buffer.from(raw, "base64").toString("utf8") : raw;
  const headers = event.headers || {};
  const contentType = String(headers["content-type"] ?? headers["Content-Type"] ?? "").toLowerCase();

  if (contentType.includes("application/x-www-form-urlencoded")) {
    const body = {};
    for (const [key, value] of new URLSearchParams(decoded)) body[key] = value;
    return { body };
  }

  try {
    return { body: JSON.parse(decoded) };
  } catch {
    return { error: { code: "INVALID_JSON", message: "Request body is not valid JSON" } };
  }
}

export function occurrenceModeOf(body) {
  if (
    body.occurrenceMode !== undefined &&
    body.occurrence_mode !== undefined &&
    String(body.occurrenceMode).trim().toLowerCase() !==
      String(body.occurrence_mode).trim().toLowerCase()
  ) {
    return {
      error: {
        code: "INVALID_OCCURRENCE_MODE",
        message: "Fields 'occurrenceMode' and 'occurrence_mode' disagree; send only occurrenceMode",
      },
    };
  }
  return { occurrenceMode: body.occurrenceMode ?? body.occurrence_mode };
}

/** Keep this behavior identical to upload-lambda/body-parser.mjs. */
export function parseDocumentTypes(documentType, occurrenceMode) {
  const hasExplicitMode = !(
    occurrenceMode === undefined || occurrenceMode === null || occurrenceMode === ""
  );
  if (
    hasExplicitMode &&
    (typeof occurrenceMode !== "string" ||
      !["single", "multiple"].includes(occurrenceMode.trim().toLowerCase()))
  ) {
    return {
      error: {
        code: "INVALID_OCCURRENCE_MODE",
        message: "Field 'occurrenceMode' must be 'single' or 'multiple'",
      },
    };
  }
  const resolvedOccurrenceMode = hasExplicitMode
    ? occurrenceMode.trim().toLowerCase()
    : null;
  if (documentType === undefined || documentType === null || documentType === "") {
    return { types: [], counts: {}, occurrenceMode: resolvedOccurrenceMode };
  }
  if (typeof documentType !== "string") {
    return {
      error: {
        code: "INVALID_DOCUMENT_TYPE",
        message:
          "Field 'documentType' must be a string — one type, or several separated " +
          "by commas (e.g. 'PEDIMENTO_ADUANAL,FACTURA_HONORARIOS'), each optionally " +
          "followed by ':N' or ':all'",
      },
    };
  }

  const types = [];
  const counts = {};
  for (const part of documentType.split(",")) {
    const item = part.trim();
    if (!item) continue;
    const [, rawType, rawCount] = TYPE_COUNT_SPLIT.exec(item);
    const type = rawType.trim();
    if (!DOCUMENT_TYPE_SHAPE.test(type)) {
      return {
        error: {
          code: "INVALID_DOCUMENT_TYPE",
          message:
            `Document type '${type || item}' must match ^[A-Z][A-Z0-9_]{0,63}$ ` +
            "(UPPER_SNAKE_CASE), optionally followed by ':N' (how many to look for) " +
            "or ':all'. Several types go in one 'documentType' separated by commas",
        },
      };
    }

    let count;
    if (rawCount !== undefined) {
      const trimmed = rawCount.trim();
      if (trimmed.toLowerCase() === "all") {
        count = "all";
      } else {
        count = /^\d+$/.test(trimmed) ? Number(trimmed) : NaN;
        if (!Number.isInteger(count) || count < 1) {
          return {
            error: {
              code: "INVALID_DOCUMENT_TYPE",
              message:
                `Count ':${rawCount}' on '${type}' must be a whole number of 1 or more, ` +
                "or 'all'",
            },
          };
        }
      }
    }

    if (!types.includes(type)) {
      types.push(type);
      counts[type] = count;
    } else {
      const seen = counts[type];
      if (seen === undefined) counts[type] = count;
      else if (count === undefined) counts[type] = seen;
      else counts[type] = seen === "all" || count === "all" ? "all" : Math.max(seen, count);
    }
  }

  if (types.length > MAX_DOCUMENT_TYPES) {
    return {
      error: {
        code: "INVALID_DOCUMENT_TYPE",
        message:
          `Field 'documentType' declares ${types.length} types; at most ${MAX_DOCUMENT_TYPES} ` +
          "are allowed (they travel as one segment of the S3 key)",
      },
    };
  }
  const joinedLength = types.join(",").length;
  if (joinedLength > MAX_DOCUMENT_TYPES_CHARS) {
    return {
      error: {
        code: "INVALID_DOCUMENT_TYPE",
        message:
          `Field 'documentType' is ${joinedLength} characters of type names; at most ` +
          `${MAX_DOCUMENT_TYPES_CHARS} are allowed (they travel as one segment of the S3 key)`,
      },
    };
  }
  if (resolvedOccurrenceMode) {
    for (const type of types) {
      if (counts[type] === undefined) {
        counts[type] = resolvedOccurrenceMode === "multiple" ? "all" : 1;
      }
    }
  }
  for (const type of types) {
    if (counts[type] === undefined) delete counts[type];
  }
  return { types, counts, occurrenceMode: resolvedOccurrenceMode };
}

export function serializeDocumentCounts(counts) {
  return Object.entries(counts || {})
    .map(([type, count]) => `${type}=${count}`)
    .join(",");
}

/** Decode trusted document-counts metadata for an inherited reprocess. */
export function deserializeDocumentCounts(raw, allowedTypes = []) {
  const allowed = new Set(allowedTypes);
  const counts = {};
  for (const entry of String(raw || "").split(",")) {
    const [type, value, ...rest] = entry.split("=");
    if (rest.length || !allowed.has(type)) continue;
    if (value === "all") counts[type] = "all";
    else if (/^\d+$/.test(value) && Number(value) >= 1) counts[type] = Number(value);
  }
  return counts;
}

export function classificationModeFor(types) {
  if (types.length > 1) return "guided";
  return types.length ? "manual" : "auto";
}

export function rawKeyFor(types, documentId, fileName) {
  const declared = types.join(",");
  return declared
    ? `raw/${declared}/${documentId}/${fileName}`
    : `raw/${documentId}/${fileName}`;
}

/** Recover the caller declaration from the pending row created at upload. */
export function inheritedDocumentType(item) {
  const stored = String(item?.RequestedDocumentType || "").trim();
  if (stored && stored !== "AUTO") return stored;

  // Older pending rows may only carry UploadKey. raw/{TYPE[,TYPE]}/{id}/file
  // has four path parts; raw/{id}/file (automatic) has three.
  const parts = String(item?.UploadKey || "").split("/").filter(Boolean);
  return parts.length >= 4 && parts[0] === "raw" ? parts.at(-3) : "";
}

export function validateSourceFile(fileName, types) {
  const parts = String(fileName || "").split(".");
  const ext = parts.length > 1 ? parts.pop().toLowerCase() : "";
  if (!ext || !ALLOWED_EXTENSIONS.has(ext)) {
    return {
      error: {
        code: "INVALID_FORMAT",
        message: `File format '${ext || "unknown"}' is not supported. Allowed: jpg, jpeg, png, pdf, xml, xlsx, csv`,
      },
    };
  }
  if (REQUIRES_DECLARED_TYPE.has(ext) && !types.length) {
    return {
      error: {
        code: "DOCUMENT_TYPE_REQUIRED",
        message:
          `Field 'documentType' is required to reprocess .${ext} files — there is no ` +
          "vision-based auto-classification for structured data sources",
      },
    };
  }
  if (REQUIRES_DECLARED_TYPE.has(ext) && types.length > 1) {
    return {
      error: {
        code: "INVALID_DOCUMENT_TYPE",
        message:
          `Field 'documentType' must name exactly one type for .${ext} files — ` +
          "structured sources cannot be classified among several schemas",
      },
    };
  }
  return { ext };
}

/**
 * Build only the caller-owned metadata for the new object.
 *
 * Once DynamoDB has a Metadata attribute it is authoritative even when it is
 * `{}`: an empty map may mean PATCH deliberately deleted every old key, so
 * falling back to the immutable S3 metadata would resurrect deleted values.
 */
export function userMetadataForReprocess(
  sourceMetadata = {},
  currentMetadata = {},
  currentIsAuthoritative = false
) {
  const out = {};
  const add = (entries) => {
    if (!entries || typeof entries !== "object" || Array.isArray(entries)) return;
    for (const [key, value] of Object.entries(entries)) {
      if (!SYSTEM_OBJECT_METADATA.has(key) && value !== null && value !== undefined && value !== "") {
        out[key] = String(value);
      }
    }
  };
  if (!currentIsAuthoritative) add(sourceMetadata);
  add(currentMetadata);
  return out;
}
