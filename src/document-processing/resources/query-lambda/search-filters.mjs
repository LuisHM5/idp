/**
 * Search filtering for `GET /documents` — query-string parsing and the
 * matching rules it feeds.
 *
 * Its own module, with no AWS imports, so it can be unit-tested without
 * pulling the SDK (which the Lambda runtime provides and this repo does not
 * install locally) — same reason upload-lambda keeps `body-parser.mjs` apart.
 * Pure throughout: query-string maps and result envelopes in, booleans and
 * plain objects out.
 *
 * The filters are ANDed, so one request answers "this document type, from this
 * system, with this upload metadata, and this extracted value". They differ in
 * where they read from, which is also their cost order — cheapest first, which
 * is the order `query()` applies them in:
 *
 *   system      envelope.tracking.appOrigin — one known key
 *   meta.<k>    envelope.metadata           — one known key
 *   field/value a dot-path, else a deep walk of the whole envelope
 *
 * See index.mjs's module docstring for the endpoint contract these implement.
 */

/** Resolve a dot-path (no array indices) against an object. */
export function resolvePath(obj, path) {
  let cur = obj;
  for (const key of path.split(".")) {
    if (cur == null || typeof cur !== "object") return undefined;
    cur = cur[key];
  }
  return cur;
}

export function norm(v) {
  return String(v == null ? "" : v).trim().toLowerCase();
}

export function compare(actual, expected, op) {
  if (actual == null) return false;
  const a = norm(actual);
  const b = norm(expected);
  return op === "contains" ? a.includes(b) : a === b;
}

/**
 * The comparable content of a field-tree node.
 *
 * A field-centric-DSL (schema_version 4.x) result wraps every scalar as
 * `{value, confidence, valid, reason, source}`, so the raw node is an object
 * and comparing it stringifies to "[object Object]" — it never matches. Only
 * fields that ALSO appear as plain scalars in the compact per-document map
 * were findable, which excludes anything inside an array row: once
 * PEDIMENTO_ERP moved its provider/invoice data into `FACTURAS[]`, the
 * documented `?field=PROVEEDOR_NOMBRE&value=...` search stopped matching.
 * Unwrapping here restores it for any nested field, however deep.
 */
function comparable(node) {
  if (node && typeof node === "object" && !Array.isArray(node) && "value" in node) {
    return node.value;
  }
  return node;
}

/**
 * Deep search: does any key named `leaf` anywhere in `obj` match `value`?
 * Returns the first matching value, or undefined.
 */
export function deepFind(obj, leaf, expected, op) {
  const stack = [obj];
  while (stack.length) {
    const cur = stack.pop();
    if (Array.isArray(cur)) {
      for (const el of cur) if (el && typeof el === "object") stack.push(el);
    } else if (cur && typeof cur === "object") {
      for (const [k, v] of Object.entries(cur)) {
        if (k === leaf) {
          const candidate = comparable(v);
          if (candidate !== null && typeof candidate !== "object" && compare(candidate, expected, op)) {
            return candidate;
          }
        }
        if (v && typeof v === "object") stack.push(v);
      }
    }
  }
  return undefined;
}

/** Try exact dot-path first, then a deep search by the leaf key. */
export function matchField(envelope, path, value, op) {
  const direct = comparable(resolvePath(envelope, path));
  if (direct !== undefined && direct !== null && typeof direct !== "object") {
    if (compare(direct, value, op)) return { matched: true, value: direct, where: path };
    return { matched: false };
  }
  const leaf = path.split(".").pop();
  const found = deepFind(envelope, leaf, value, op);
  if (found !== undefined) return { matched: true, value: found, where: `*.${leaf}` };
  return { matched: false };
}

/** `tracking.appOrigin` on a result envelope — see handler.py. */
export function documentAppOrigin(envelope) {
  return (envelope && envelope.tracking && envelope.tracking.appOrigin) || null;
}

// --------------------------------------------------- query-string parsing

/**
 * Every value API Gateway received for `name`, in the order they appeared.
 *
 * A REST proxy integration delivers a repeated query parameter twice: only the
 * LAST value in `queryStringParameters`, and all of them in
 * `multiValueQueryStringParameters`. Reading the multi-value map first is what
 * lets `?field=A&value=1&field=B&value=2` mean two filters instead of silently
 * keeping just the last pair. Falls back to the single-value map so a direct
 * Lambda invoke or a test event (which rarely bothers building the multi-value
 * one) behaves exactly as it did before.
 */
export function paramList(params, multi, name) {
  const values = multi && multi[name];
  if (Array.isArray(values)) return values.filter((v) => v != null && v !== "");
  const single = params[name];
  return single == null || single === "" ? [] : [single];
}

/** Comma-separated list, trimmed, empties dropped: "A, B" -> ["A","B"]. */
export function splitList(values) {
  return values
    .flatMap((v) => String(v).split(","))
    .map((v) => v.trim())
    .filter(Boolean);
}

/**
 * The `field`/`value`/`op` triples, paired by position in the URL.
 *
 * Several filters are ANDed — a document must satisfy every one of them, which
 * is what makes "this type, from this system, with this metadata, and this
 * extracted value" a single request. `op` may be given once (it then applies
 * to every filter) or once per filter; any other count is a caller mistake
 * worth a 400 rather than a silent guess about which field the lone `op` was
 * meant for.
 *
 * Returns `{ filters }` or `{ error: { code, message } }`.
 */
export function buildFilters(params, multi) {
  let fields = paramList(params, multi, "field");
  if (!fields.length) fields = paramList(params, multi, "path");
  const values = paramList(params, multi, "value");
  const ops = paramList(params, multi, "op");

  if (!fields.length && !values.length) return { filters: [] };
  if (fields.length && !values.length) {
    return { error: { code: "MISSING_PARAMS", message: "'field' requires 'value'" } };
  }
  if (fields.length !== values.length) {
    return {
      error: {
        code: "MISSING_PARAMS",
        message:
          `Got ${fields.length} 'field' and ${values.length} 'value' parameters — each ` +
          "'field' needs exactly one 'value', paired in the order they appear in the URL",
      },
    };
  }
  if (ops.length > 1 && ops.length !== fields.length) {
    return {
      error: {
        code: "INVALID_PARAMS",
        message:
          `Got ${ops.length} 'op' parameters for ${fields.length} filter(s) — pass 'op' ` +
          "once (it then applies to all of them) or once per filter",
      },
    };
  }
  const filters = [];
  for (let i = 0; i < fields.length; i++) {
    const field = String(fields[i]).trim();
    if (!field) continue;
    const op = ops.length === 1 ? ops[0] : ops[i];
    filters.push({ field, value: values[i], op: op === "contains" ? "contains" : "eq" });
  }
  return { filters };
}

/**
 * `?meta.<clave>=<valor>` — the caller's own upload metadata (see
 * upload-lambda's `normalizeMetadata`), which is context this system never
 * interprets: a lote, a folio in the caller's ERP, a branch code.
 *
 * Keys are lowercased to match how S3 (and therefore the envelope) stored
 * them, so `meta.Lote` and `meta.lote` are the same filter. Repeating one key
 * ORs its values (`?meta.lote=A&meta.lote=B` = "either lote"); different keys
 * are ANDed, like every other filter here.
 */
export function buildMetaFilters(params, multi) {
  const byKey = new Map();
  const names = new Set([...Object.keys(params || {}), ...Object.keys(multi || {})]);
  for (const name of names) {
    if (!name.startsWith("meta.")) continue;
    const key = name.slice("meta.".length).trim().toLowerCase();
    if (!key) continue;
    const values = splitList(paramList(params, multi, name));
    if (!values.length) continue;
    byKey.set(key, [...new Set([...(byKey.get(key) || []), ...values])]);
  }
  return [...byKey.entries()]
    .map(([key, values]) => ({ key, values }))
    .sort((a, b) => a.key.localeCompare(b.key));
}

/**
 * `?system=` — which app uploaded the document (`tracking.appOrigin`, written
 * by the SQS consumer from the `app-origin` S3 metadata upload-lambda resolved
 * from the caller's x-api-key). Several systems are ORed.
 *
 * Note this is orthogonal to `AccessScope: "own"`, which already pins an app
 * to its own documents: for such an app this filter can only narrow further,
 * never widen. It is the `"all"`-scope apps (an admin console, a reporting
 * job) that use it to ask "only what the ERP sent".
 */
export function buildSystems(params, multi) {
  return [
    ...new Set(
      splitList([
        ...paramList(params, multi, "system"),
        ...paramList(params, multi, "appOrigin"),
        ...paramList(params, multi, "sistema"),
      ])
    ),
  ];
}

/** Does this document come from one of the requested systems? */
export function matchesSystems(envelope, systems) {
  if (!systems.length) return true;
  const origin = documentAppOrigin(envelope);
  if (origin == null) return false;
  return systems.some((s) => norm(origin) === norm(s));
}

/**
 * Does this document satisfy every `meta.<clave>` filter?
 *
 * The envelope's own keys are lowercased again on the way in: normalizeMetadata
 * has done it at the source since this feature shipped, but a document
 * processed before that (or written by a backfill from raw S3 metadata) can
 * still carry mixed case, and a filter must not miss it over that.
 */
export function matchesMeta(envelope, metaFilters, op) {
  if (!metaFilters.length) return true;
  const meta = (envelope && envelope.metadata) || {};
  if (typeof meta !== "object") return false;
  const lowered = {};
  for (const [k, v] of Object.entries(meta)) lowered[String(k).toLowerCase()] = v;
  return metaFilters.every(({ key, values }) =>
    values.some((v) => compare(lowered[key], v, op))
  );
}

