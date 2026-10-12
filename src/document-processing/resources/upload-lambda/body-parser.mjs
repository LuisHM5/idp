/**
 * Request-body parsing for the upload endpoint — JSON and
 * `application/x-www-form-urlencoded`.
 *
 * Its own module, with no AWS imports, so it can be unit-tested without
 * pulling the SDK (which the Lambda runtime provides and this repo does not
 * install locally). Pure: takes an API Gateway event, returns a plain object.
 */

/**
 * Parse the request body according to its Content-Type.
 *
 * The form encoding exists because plenty of callers (SAP, older .NET/Java
 * HTTP stacks, a plain `curl -d`) send forms far more easily than they build
 * JSON. A form value is always a string, so `metadata` is accepted there as a
 * JSON-encoded string — there is no other way to express a nested object in a
 * form body.
 *
 * Content-Type decides, defaulting to JSON when the header is absent, so
 * existing callers are unaffected.
 *
 * Returns `{ body }` on success or `{ error: {code, message} }` — the caller
 * turns that into an HTTP response, keeping this module free of any HTTP
 * shape of its own.
 */
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
    // `metadata` is the only non-scalar field the endpoint accepts.
    if (typeof body.metadata === "string" && body.metadata.trim()) {
      try {
        body.metadata = JSON.parse(body.metadata);
      } catch {
        return {
          error: {
            code: "INVALID_METADATA",
            message:
              "Field 'metadata' must be a JSON object encoded as a string in form bodies",
          },
        };
      }
    }
    return { body };
  }

  try {
    return { body: JSON.parse(decoded) };
  } catch {
    return { error: { code: "INVALID_JSON", message: "Request body is not valid JSON" } };
  }
}

/**
 * S3 object-metadata keys this system owns; a caller's own `metadata` may not
 * write them. `app-origin` is the traceability signal the query API filters on
 * (`GET /documents?system=`) and it is resolved from the request's OWN
 * x-api-key — letting a body field set it would let any app claim to be
 * another one. `model-id` has its own top-level `modelId` field,
 * `extraction-mode` has its own top-level `extractionMode` field, y
 * `document-counts` transporta la cardinalidad resuelta desde `occurrenceMode`
 * y los sufijos `:N`/`:all` de `documentType`: si un caller pudiera escribirla
 * a mano podría pedir un tipo y hacer que el pipeline buscara otro número de
 * documentos del que declaró.
 */
const RESERVED_METADATA_KEYS = new Set([
  "app-origin",
  "model-id",
  "extraction-mode",
  "document-counts",
]);

export const EXTRACTION_MODES = new Set(["catalog", "model", "hybrid", "deterministic"]);

/** Validate the per-upload extraction strategy without coupling it to HTTP. */
export function parseExtractionMode(value) {
  if (value === undefined || value === null || value === "") return { mode: "catalog" };
  if (typeof value !== "string" || !EXTRACTION_MODES.has(value.trim().toLowerCase())) {
    return {
      error: {
        code: "INVALID_EXTRACTION_MODE",
        message:
          "Field 'extractionMode' must be 'catalog', 'model', 'hybrid' or 'deterministic'",
      },
    };
  }
  return { mode: value.trim().toLowerCase() };
}

/** S3's ceiling for all user metadata on one object (keys + values together). */
const MAX_METADATA_BYTES = 2048;

/** Keys have to survive an HTTP header name AND a `?meta.<clave>=` query param
 * (see the search endpoint), so they are lowercase alphanumerics plus `.`, `_`
 * and `-`. Rejecting a key with a space HERE is what keeps it from becoming an
 * opaque S3 error at the presigned PUT. */
const METADATA_KEY_SHAPE = /^[a-z0-9][a-z0-9._-]*$/;

/**
 * Normalize the caller's `metadata` into what S3 can actually store, failing
 * loudly HERE instead of at the client's `PUT` — every constraint below
 * otherwise surfaces as an opaque S3 error minutes later and one hop away
 * (non-ASCII mangles the presigned signature into `403 SignatureDoesNotMatch`,
 * which reads like a permissions bug; oversized metadata is a `400
 * MetadataTooLarge` against a URL this Lambda handed out as valid).
 *
 *   - Keys are lowercased, because S3 lowercases them on the way in anyway.
 *     Doing it here means `metadata: {"Lote": ...}` and `?meta.lote=` agree
 *     instead of the caller having to guess which casing survived the trip.
 *   - Reserved keys are dropped (see RESERVED_METADATA_KEYS) — silently, not
 *     with a 400: they are this system's own, so a caller that happens to use
 *     the name is not making an error worth failing an upload over.
 *   - Scalars become strings; `null`/`undefined`/`""` entries are dropped.
 *   - Objects, arrays, non-ASCII and >2KB total are rejected: S3 user
 *     metadata is an HTTP header, and it holds neither structure nor UTF-8.
 *
 * Returns `{ metadata }` or `{ error: {code, message} }`, same contract as
 * `parseBody` — this module stays free of any HTTP shape of its own.
 *
 * ⚠️ These rules must stay identical to query-lambda's `metadata-rules.mjs`,
 * which applies the same contract at `PATCH /documents/{id}/metadata`. The two
 * Lambdas bundle from separate asset directories with a plain `cp`, so there is
 * no way to import one from the other; `tests/unit/node/metadata-rules.test.mjs`
 * runs BOTH against one shared table of cases so the parity is a test and not a
 * comment.
 */
export function normalizeMetadata(metadata) {
  if (metadata === undefined || metadata === null) return { metadata: {} };
  if (typeof metadata !== "object" || Array.isArray(metadata)) {
    return {
      error: {
        code: "INVALID_METADATA",
        message: "Field 'metadata' must be a JSON object",
      },
    };
  }

  const out = {};
  let bytes = 0;
  for (const [rawKey, rawValue] of Object.entries(metadata)) {
    const key = String(rawKey).trim().toLowerCase();
    if (!key || RESERVED_METADATA_KEYS.has(key)) continue;
    if (!METADATA_KEY_SHAPE.test(key)) {
      return {
        error: {
          code: "INVALID_METADATA",
          message:
            `Metadata key '${key}' must be lowercase alphanumerics plus '.', '_' or '-' ` +
            "(it has to work both as an S3 metadata header and as a ?meta.<key>= filter)",
        },
      };
    }
    if (rawValue === null || rawValue === undefined) continue;
    if (typeof rawValue === "object") {
      return {
        error: {
          code: "INVALID_METADATA",
          message:
            `Field 'metadata.${key}' must be a string, number or boolean — S3 object ` +
            "metadata is a flat set of headers and cannot hold nested objects or arrays",
        },
      };
    }
    const value = String(rawValue).trim();
    if (!value) continue;
    if (!/^[\x20-\x7e]*$/.test(value)) {
      return {
        error: {
          code: "INVALID_METADATA",
          message:
            `Field 'metadata.${key}' must be US-ASCII — S3 stores user metadata as an ` +
            "HTTP header, and a non-ASCII byte there breaks the presigned signature",
        },
      };
    }
    bytes += key.length + value.length;
    out[key] = value;
  }

  if (bytes > MAX_METADATA_BYTES) {
    return {
      error: {
        code: "INVALID_METADATA",
        message:
          `Field 'metadata' is ${bytes} bytes; S3 caps user metadata at ${MAX_METADATA_BYTES} ` +
          "for the whole object (keys and values together)",
      },
    };
  }
  return { metadata: out };
}

/** Tipo documental: UPPER_SNAKE_CASE, empieza con letra. */
const DOCUMENT_TYPE_SHAPE = /^[A-Z][A-Z0-9_]{0,63}$/;

/**
 * How many types one upload may declare, and how long the joined string may
 * get. Both exist for the same reason: the declared types travel as ONE S3 key
 * segment (`raw/{A,B,C}/{uuid}/{file}`), and an S3 key is capped at 1024 bytes
 * for the whole path — uuid and filename included. 8 types is already far more
 * than any real expediente needs, and 200 chars leaves the rest of the key
 * plenty of room.
 */
const MAX_DOCUMENT_TYPES = 8;
const MAX_DOCUMENT_TYPES_CHARS = 200;

/** El sufijo `:N` / `:all`. El TIPO se valida aparte, con
 * `DOCUMENT_TYPE_SHAPE`, para que siga exigiendo UPPER_SNAKE_CASE: un regex
 * combinado con el flag `i` aceptaría `pedimento_erp` en minúsculas, que es
 * justo lo que el mensaje de error dice que no se acepta. */
const TYPE_COUNT_SPLIT = /^([^:]*)(?::(.*))?$/;

/**
 * Parse the `documentType` field into the types the caller declared and how
 * many documents of each to look for.
 *
 * ONE field, comma-separated — deliberately not a second `documentTypes`
 * array. It is the same convention the search endpoint already uses
 * (`GET /documents?docType=A,B`), it needs no new field in the contract, and
 * it works identically in a JSON body and in a form body, where every value is
 * a string and an array could only travel as JSON-in-a-string.
 *
 *   PEDIMENTO_ERP                       -> catálogo; luego default global single
 *   PEDIMENTO_ERP:2                     -> hasta 2
 *   FACTURA_FLETE_EXTRANJERO:all        -> todas las que haya
 *   PEDIMENTO_ERP,FACTURA_HONORARIOS    -> catálogos; luego default global multiple
 *   PEDIMENTO_ERP:1,FACTURA_HONORARIOS  -> override del primero; modo para el segundo
 *
 * A single type still means "honour it and never re-segment the file"; two or
 * more switch the pipeline into guided classification, where it segments but
 * may only choose among the declared types.
 *
 * Si el POST omite `occurrenceMode`, las cuentas sin sufijo se dejan sin
 * resolver: enrichment aplicará primero el default del catálogo y luego el
 * default global (uno -> `single`, varios -> `multiple`). Un modo explícito se
 * materializa aquí para todos los tipos sin sufijo, y un sufijo siempre gana.
 *
 * Los TIPOS se devuelven aparte de las CUENTAS a propósito: los tipos viajan en
 * la llave de S3 (`raw/{A,B}/{uuid}/file`, que parsean tres componentes
 * independientes) y las cuentas viajan como metadata del objeto, que solo lee
 * el enrichment. Meter `:` en la llave sería un carácter de la lista "requiere
 * manejo especial" de AWS, firmado en un presigned PUT, por un dato que a esos
 * tres lectores no les sirve.
 *
 * Un tipo repetido se colapsa conservando el orden de primera aparición y **la
 * cuenta MAYOR**, para que una lista mal escrita nunca descarte documentos en
 * silencio.
 *
 * Returns `{ types, counts, occurrenceMode }` or `{ error: {code, message} }`,
 * same contract as `parseBody`/`normalizeMetadata` — no HTTP shape here.
 */
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
  let resolvedOccurrenceMode = hasExplicitMode
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

    // `undefined` significa "sin sufijo". Se resuelve después de deduplicar los
    // tipos, usando el occurrenceMode de ESTE POST.
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
      // Repetido: gana la cuenta mayor, nunca la última escrita.
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

/**
 * Las cuentas por tipo, serializadas para viajar como metadata de objeto S3.
 *
 * `PEDIMENTO_ERP=1,FACTURA_FLETE_EXTRANJERO=all` — plano y US-ASCII, que es
 * todo lo que S3 admite en un valor de metadata (ver `normalizeMetadata`).
 * Deliberadamente NO viaja en la llave junto a los tipos: la llave la parsean
 * tres componentes independientes (el consumidor de SQS, model-invoke y el
 * enrichment) y se firma en un presigned PUT, mientras que las cuentas solo las
 * lee el enrichment.
 *
 * Devuelve "" cuando no hay nada que declarar, para que el caller omita la
 * clave en vez de mandar una vacía.
 */
export function serializeDocumentCounts(counts) {
  return Object.entries(counts || {})
    .map(([type, count]) => `${type}=${count}`)
    .join(",");
}

/**
 * How the pipeline will decide this document's type, from what the caller
 * declared. Returned to the caller as `classificationMode`.
 *
 *   "manual" — exactly one type: honoured as-is, never re-segmented.
 *   "guided" — several: the file IS segmented and classified, but only among
 *              the declared types; other segments are omitted from the result.
 *   "auto"   — none: the classifier chooses from the whole catalog.
 *
 * "guided" is a new value of an existing enum, so a caller that never sends a
 * list never sees it.
 */
export function classificationModeFor(types) {
  if (types.length > 1) return "guided";
  return types.length ? "manual" : "auto";
}

/**
 * The S3 key the document is uploaded to.
 *
 * The declared types travel as ONE path segment, comma-joined — this key is
 * their only carrier, read back independently by the SQS consumer, the
 * model-invoke Lambda and the enrichment Lambda. With none declared the type
 * segment is absent entirely, which is exactly how those three readers tell
 * "auto-classify" from "the uploader said so".
 */
export function rawKeyFor(types, documentId, fileName) {
  const declared = types.join(",");
  return declared
    ? `raw/${declared}/${documentId}/${fileName}`
    : `raw/${documentId}/${fileName}`;
}
