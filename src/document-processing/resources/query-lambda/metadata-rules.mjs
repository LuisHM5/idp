/**
 * The metadata contract, on the write side of `PATCH /documents/{id}/metadata`.
 *
 * ⚠️ The RULES here must stay identical to upload-lambda's
 * `body-parser.mjs::normalizeMetadata`, which applies the same contract at
 * `POST /documents`. They are one contract seen at two doors: a key a caller
 * can set at upload but not patch (or the reverse) is a bug, not a feature.
 * The two Lambdas are bundled from separate asset directories with a plain
 * `cp` — there is no bundler to follow a cross-directory import — so the code
 * is duplicated on purpose. `tests/unit/node/metadata-rules.test.mjs` runs
 * BOTH implementations against one shared table of cases, so the guarantee is
 * a test and not this comment.
 *
 * What differs here is only the SHAPE of the operation: `POST` takes a whole
 * bag of metadata, `PATCH` takes a JSON Merge Patch (RFC 7386) over the
 * metadata a document already has.
 *
 * Its own module, with no AWS imports, so it can be unit-tested without
 * pulling the SDK — same split as `search-filters.mjs`.
 */

/** Keys this system owns; a caller may not write them. See body-parser.mjs. */
export const RESERVED_METADATA_KEYS = new Set([
  "app-origin",
  "model-id",
  "extraction-mode",
  "ai-response-format",
  "document-counts",
]);

/** S3's ceiling for all user metadata on one object (keys + values together).
 * Patched metadata never goes back to S3 — it lives on the DynamoDB item — but
 * the cap stays the same on purpose: one rule for one contract beats "what you
 * couldn't upload, you can patch in". */
export const MAX_METADATA_BYTES = 2048;

/** Printable US-ASCII. S3 stores user metadata as an HTTP header. */
export const ASCII_ONLY = /^[\x20-\x7e]*$/;

/** Keys have to survive an HTTP header name AND a `?meta.<clave>=` query
 * param, so they are lowercase alphanumerics plus `.`, `_` and `-`. Rejecting
 * a key with a space here is what keeps it from becoming an opaque S3 error at
 * the presigned PUT. */
export const METADATA_KEY_SHAPE = /^[a-z0-9][a-z0-9._-]*$/;

/** Total size of a metadata object, counted the way MAX_METADATA_BYTES is. */
export function metadataBytes(metadata) {
  let bytes = 0;
  for (const [key, value] of Object.entries(metadata || {})) {
    bytes += key.length + String(value).length;
  }
  return bytes;
}

/** `{code, message}` for the caller to turn into an HTTP response. */
function invalid(message) {
  return { error: { code: "INVALID_METADATA", message } };
}

/**
 * Validate one key/value the way both doors do.
 *
 * Returns `{ key }` for a deletion, `{ key, value }` for a write, `null` when
 * the entry is to be ignored (a reserved key), or `{ error }`.
 */
export function normalizeEntry(rawKey, rawValue) {
  const key = String(rawKey).trim().toLowerCase();
  if (!key) return null;
  if (RESERVED_METADATA_KEYS.has(key)) return null;
  if (!METADATA_KEY_SHAPE.test(key)) {
    return invalid(
      `Metadata key '${key}' must be lowercase alphanumerics plus '.', '_' or '-' ` +
        "(it has to work both as an S3 metadata header and as a ?meta.<key>= filter)"
    );
  }
  // null / undefined / "" all mean "no value": a deletion on the PATCH side,
  // an entry to skip on the upload side.
  if (rawValue === null || rawValue === undefined) return { key };
  if (typeof rawValue === "object") {
    return invalid(
      `Field 'metadata.${key}' must be a string, number or boolean — S3 object ` +
        "metadata is a flat set of headers and cannot hold nested objects or arrays"
    );
  }
  const value = String(rawValue).trim();
  if (!value) return { key };
  if (!ASCII_ONLY.test(value)) {
    return invalid(
      `Field 'metadata.${key}' must be US-ASCII — S3 stores user metadata as an ` +
        "HTTP header, and a non-ASCII byte there breaks the presigned signature"
    );
  }
  return { key, value };
}

/**
 * Apply a JSON Merge Patch (RFC 7386) to the metadata a document already has.
 *
 * Merge-patch semantics, and they are the whole point of this being a PATCH
 * and not a PUT: a key with a value **sets** it, a key with `null` (or `""`)
 * **deletes** it, and a key that is simply absent is **left alone**. That last
 * rule is what lets two apps track different keys on the same document without
 * ever overwriting each other.
 *
 * Returns `{ set, remove, next }` — the keys to write, the keys to delete, and
 * the resulting metadata — or `{ error: {code, message} }`.
 */
export function parseMetadataPatch(patch, current = {}) {
  if (patch === null || patch === undefined || typeof patch !== "object" || Array.isArray(patch)) {
    return invalid(
      "The request body must be a flat JSON object of metadata keys, e.g. " +
        '{"estado": "EN_REVISION", "folio_erp": "4711"}. A key with null deletes it.'
    );
  }

  // Keep the last operation for a normalized key. JSON may legally contain
  // both `Estado` and `estado`; after lowercasing they are one key, so source
  // order must decide whether the final operation sets or deletes it.
  const operations = new Map();
  let sawReserved = false;
  for (const [rawKey, rawValue] of Object.entries(patch)) {
    const entry = normalizeEntry(rawKey, rawValue);
    if (entry === null) {
      sawReserved = true;
      continue;
    }
    if (entry.error) return entry;
    operations.set(
      entry.key,
      entry.value === undefined ? { kind: "remove" } : { kind: "set", value: entry.value }
    );
  }

  const set = {};
  const remove = [];
  for (const [key, operation] of operations) {
    if (operation.kind === "remove") remove.push(key);
    else set[key] = operation.value;
  }

  if (!Object.keys(set).length && !remove.length) {
    return {
      error: {
        code: "EMPTY_PATCH",
        message: sawReserved
          ? `The patch only names keys this system owns (${[...RESERVED_METADATA_KEYS].join(", ")}), ` +
            "which are never writable by a caller — 'app-origin' comes from your own API key"
          : "The patch has no keys to set or delete",
      },
    };
  }

  const next = { ...current };
  for (const key of remove) delete next[key];
  Object.assign(next, set);

  const bytes = metadataBytes(next);
  if (bytes > MAX_METADATA_BYTES) {
    return invalid(
      `The resulting metadata would be ${bytes} bytes; the cap is ${MAX_METADATA_BYTES} for the ` +
        "whole document (keys and values together). Delete a key in the same patch to make room."
    );
  }
  return { set, remove, next };
}
