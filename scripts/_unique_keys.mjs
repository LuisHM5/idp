/**
 * Unique keys declared in a catalog entry — derivation + normalization,
 * shared by every Node-side consumer so the rule lives in ONE place:
 *
 *   - scripts/sync-catalogs.mjs  → bakes `uniqueKeys` into
 *     query-lambda/document-types.json at build time
 *   - scripts/sync-schema.mjs    → writes the derived `unique_keys` attribute
 *     on each DynamoDB catalog row, so the query Lambda can project it
 *     without dragging the whole `fields` map along
 *   - scripts/backfill-keys.mjs  → rebuilds the lookup table from stored
 *     EnrichmentResults
 *
 * The Python authority for the same rules is
 * `enrichment-lambda/document_catalog.py::_parse_unique_keys` (which fields
 * are keys) and `enrichment-lambda/unique_keys.py::normalize_value` /
 * `build_key` (what the key looks like). The query Lambda carries its own
 * copy of the normalizer for the same reason — a Lambda asset folder can't
 * import from `scripts/`. All four must agree; change one, change the rest.
 */

export const UNIQUE_KEY_NORMALIZERS = ["upper", "alnum", "digits", "raw"];
export const DEFAULT_UNIQUE_KEY_NORMALIZER = "upper";
export const KEY_SEPARATOR = "#";

/** One `{field, normalize}` spec, or null when `options` opts out. */
function spec(field, options) {
  if (options === false || options == null) return null;
  let normalize = DEFAULT_UNIQUE_KEY_NORMALIZER;
  if (typeof options === "object") {
    const candidate = String(options.normalize || "").trim().toLowerCase();
    if (UNIQUE_KEY_NORMALIZERS.includes(candidate)) normalize = candidate;
  }
  return { field: String(field).trim(), normalize };
}

/**
 * Every unique key a catalog entry declares, in lookup order: the type-level
 * `unique_key`/`unique_keys` first (the only option for legacy-DSL types),
 * then each field-centric `fields[NAME].unique_key`. Deduplicated by field
 * name, so declaring it in both places is harmless.
 *
 * Mirror of `document_catalog._parse_unique_keys`.
 */
export function uniqueKeysOf(entry) {
  const specs = [];
  const seen = new Set();
  const add = (s) => {
    if (s && s.field && !seen.has(s.field)) {
      seen.add(s.field);
      specs.push(s);
    }
  };

  const raw = entry.unique_keys !== undefined ? entry.unique_keys : entry.unique_key;
  const declared = Array.isArray(raw) ? raw : raw ? [raw] : [];
  for (const item of declared) {
    if (typeof item === "string") add(spec(item, true));
    else if (item && typeof item === "object" && item.field) add(spec(item.field, item));
  }

  for (const [name, field] of Object.entries(entry.fields || {})) {
    if (name === "$common" || !field || typeof field !== "object") continue;
    if ("unique_key" in field) add(spec(name, field.unique_key));
  }

  return specs;
}

/**
 * Deterministic value normalization — mirror of
 * `enrichment-lambda/unique_keys.py::normalize_value`.
 */
export function normalizeKeyValue(value, mode = DEFAULT_UNIQUE_KEY_NORMALIZER) {
  const text = String(value == null ? "" : value).trim().replace(/\s+/g, " ");
  if (!text) return "";
  if (mode === "raw") return text;
  const upper = text.toUpperCase();
  if (mode === "alnum") return upper.replace(/[^A-Z0-9]/g, "");
  if (mode === "digits") return upper.replace(/[^0-9]/g, "");
  return upper;
}

/** `TIPODOC#VALOR_NORMALIZADO`, or "" when there is nothing to key on. */
export function buildUniqueKey(typeId, value, mode) {
  const normalized = normalizeKeyValue(value, mode);
  const type = String(typeId || "").trim().toUpperCase();
  return normalized && type ? `${type}${KEY_SEPARATOR}${normalized}` : "";
}
