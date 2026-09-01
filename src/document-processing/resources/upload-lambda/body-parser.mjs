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
 * another one. `model-id` has its own top-level `modelId` field.
 */
const RESERVED_METADATA_KEYS = new Set(["app-origin", "model-id"]);

/** S3's ceiling for all user metadata on one object (keys + values together). */
const MAX_METADATA_BYTES = 2048;

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
    if (!/^[\x20-\x7e]*$/.test(key) || !/^[\x20-\x7e]*$/.test(value)) {
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
