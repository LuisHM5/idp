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
