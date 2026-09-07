/**
 * Pure logic for `GET /document-types` and `GET /document-types/{type}` —
 * the catalog-discovery endpoints. No AWS SDK here (mirrors search-filters.mjs
 * / reprocess-request.mjs): index.mjs does the DynamoDB Scan and file read,
 * this module only merges/paginates/shapes plain JS data, so it is
 * unit-testable without credentials.
 *
 * Same bundled-then-DynamoDB-override precedence as the Python loader
 * (document_catalog.py) and query-lambda's own `knownTypes()`: bundled
 * `document-types.json` (built by scripts/sync-catalogs.mjs from `catalogs/`)
 * is the base, and a DynamoDB row with `enabled !== false` replaces or adds a
 * type — `enabled: false` means "ignore this hot-reload row", never a hard
 * delete of the bundled type.
 */

export const DOCUMENT_TYPES_PAGE_SIZE = 5;

/** Merge bundled entries (array from document-types.json: {typeId,
 *  displayName, description, fields, ...}) with DynamoDB override rows
 *  (array of plain-JS scanned items: {type_id, display_name, description,
 *  fields, enabled}). Sorted by typeId so pagination is stable across calls. */
export function mergeDocumentTypes(bundled, overrides) {
  const byId = new Map();
  for (const t of bundled || []) {
    if (!t || !t.typeId || t.typeId.startsWith("_")) continue;
    byId.set(t.typeId, {
      typeId: t.typeId,
      displayName: t.displayName || t.typeId,
      description: t.description ?? null,
      fields: t.fields || null,
    });
  }
  for (const row of overrides || []) {
    const typeId = row && row.type_id;
    if (!typeId || typeId.startsWith("_") || row.enabled === false) continue;
    byId.set(typeId, {
      typeId,
      displayName: row.display_name || typeId,
      description: row.description ?? null,
      fields: row.fields || null,
    });
  }
  return [...byId.values()].sort((a, b) => a.typeId.localeCompare(b.typeId));
}

/** Narrow to an app's AllowedDocTypes (empty/undefined = no restriction),
 *  same "list transparently narrows instead of erroring" rule GET /documents
 *  already applies (see index.mjs's module docstring). */
export function scopeToAllowedDocTypes(types, allowedDocTypes) {
  if (!allowedDocTypes || !allowedDocTypes.length) return types;
  const allowed = new Set(allowedDocTypes.map((t) => String(t).toUpperCase()));
  return (types || []).filter((t) => allowed.has(t.typeId.toUpperCase()));
}

/** `schema` is the field-centric DSL's `fields` tree — omitted unless the
 *  caller explicitly opts in (`includeSchema`): a type like PEDIMENTO_ADUANAL
 *  has dozens of fields, so a full listing page would be dominated by schema
 *  bytes nobody asked for by default. */
function toResponseEntry(type, includeSchema) {
  return {
    type: type.typeId,
    displayName: type.displayName,
    description: type.description,
    ...(includeSchema ? { schema: type.fields || {} } : {}),
  };
}

export function parseIncludeSchema(raw) {
  return raw === "1" || raw === "true";
}

export function parsePage(raw) {
  const n = parseInt(raw, 10);
  return Number.isFinite(n) && n > 0 ? n : 1;
}

/** Page + shape the merged/scoped list for `GET /document-types`. Fixed page
 *  size (5) rather than a client-chosen `limit`: the catalog is a short,
 *  slow-changing list, not a search result set. */
export function paginateDocumentTypes(types, { page = 1, includeSchema = false } = {}) {
  const all = types || [];
  const total = all.length;
  const totalPages = Math.max(1, Math.ceil(total / DOCUMENT_TYPES_PAGE_SIZE));
  const clampedPage = Math.min(Math.max(1, Math.trunc(page) || 1), totalPages);
  const start = (clampedPage - 1) * DOCUMENT_TYPES_PAGE_SIZE;
  const items = all
    .slice(start, start + DOCUMENT_TYPES_PAGE_SIZE)
    .map((t) => toResponseEntry(t, includeSchema));
  return {
    page: clampedPage,
    pageSize: DOCUMENT_TYPES_PAGE_SIZE,
    total,
    totalPages,
    types: items,
  };
}

/** Shape a single type for `GET /document-types/{type}`. Exact TYPE_ID match
 *  only (case-insensitive) — no alias resolution: aliases are Bedrock
 *  classification hints, not a stable identifier for a schema lookup, so
 *  reusing them here would make the same string resolve differently in the
 *  two endpoints. Returns null when not found; the caller maps that to 404. */
export function findDocumentType(types, typeId, includeSchema) {
  const wanted = String(typeId || "").trim().toUpperCase();
  if (!wanted) return null;
  const found = (types || []).find((t) => t.typeId.toUpperCase() === wanted);
  return found ? toResponseEntry(found, includeSchema) : null;
}
