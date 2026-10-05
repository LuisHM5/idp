import { existsSync, readdirSync, readFileSync } from "fs";
import { basename, join, relative, resolve, sep } from "path";

export const IGNORED_CATALOG_DIRECTORIES = new Set(["save"]);

function isInsideIgnoredDirectory(relativePath) {
  return relativePath
    .split(sep)
    .slice(0, -1)
    .some((part) => IGNORED_CATALOG_DIRECTORIES.has(part));
}

function walkJsonFiles(directory, root = directory) {
  const files = [];
  for (const item of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, item.name);
    const relativePath = relative(root, path);
    if (item.isDirectory()) {
      if (!IGNORED_CATALOG_DIRECTORIES.has(item.name)) {
        files.push(...walkJsonFiles(path, root));
      }
      continue;
    }
    if (
      item.isFile() &&
      item.name.endsWith(".json") &&
      !item.name.startsWith("_") &&
      !isInsideIgnoredDirectory(relativePath)
    ) {
      files.push(path);
    }
  }
  return files;
}

export function readCatalogTypeFile(path, catalogsDir) {
  const data = JSON.parse(readFileSync(path, "utf8"));
  const entries = Object.entries(data).filter(([typeId]) => !typeId.startsWith("_"));
  const relativePath = relative(catalogsDir, path);
  if (entries.length !== 1) {
    throw new Error(
      `${relativePath}: debe declarar exactamente un TYPE_ID; encontré ${entries.length}`
    );
  }
  const [[typeId, entry]] = entries;
  const fileTypeId = basename(path, ".json");
  if (typeId !== fileTypeId) {
    throw new Error(
      `${relativePath}: la llave '${typeId}' debe coincidir con el archivo '${fileTypeId}.json'`
    );
  }
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
    throw new Error(`${relativePath}: '${typeId}' debe contener un objeto`);
  }
  return { path, relativePath, typeId, entry, data };
}

export function discoverCatalogTypes(catalogsDir) {
  const found = walkJsonFiles(catalogsDir)
    .sort((a, b) => relative(catalogsDir, a).localeCompare(relative(catalogsDir, b)))
    .map((path) => readCatalogTypeFile(path, catalogsDir));

  const byType = new Map();
  for (const item of found) {
    const previous = byType.get(item.typeId);
    if (previous) {
      throw new Error(
        `TYPE_ID duplicado '${item.typeId}': ${previous.relativePath} y ${item.relativePath}`
      );
    }
    byType.set(item.typeId, item);
  }
  return found;
}

export function isCatalogPathActive(path, catalogsDir) {
  const relativePath = relative(catalogsDir, path);
  return (
    relativePath &&
    !relativePath.startsWith(`..${sep}`) &&
    relativePath !== ".." &&
    !basename(path).startsWith("_") &&
    !isInsideIgnoredDirectory(relativePath)
  );
}

export function selectCatalogFile({ opts, positional }, types, catalogsDir) {
  if (opts.file) {
    const path = resolve(opts.file);
    if (!existsSync(path)) throw new Error(`archivo no encontrado: ${opts.file}`);
    if (!isCatalogPathActive(path, catalogsDir)) {
      throw new Error(`archivo inactivo o fuera de catalogs/: ${opts.file}`);
    }
    readCatalogTypeFile(path, catalogsDir);
    return path;
  }
  if (opts.type) {
    const match = types.find(({ typeId }) => typeId === opts.type);
    if (!match) throw new Error(`tipo no encontrado: ${opts.type}`);
    return match.path;
  }
  const needle = (opts.name || positional[0] || "").toLowerCase();
  if (!needle) throw new Error("indica un tipo: --type X, --file ruta, o un nombre");
  const matches = types.filter(({ typeId }) => typeId.toLowerCase().includes(needle));
  if (matches.length === 0) throw new Error(`ningún tipo coincide con '${needle}'`);
  if (matches.length > 1) {
    throw new Error(
      `'${needle}' es ambiguo: ${matches.map(({ typeId }) => typeId).join(", ")} — usa --type o --file`
    );
  }
  return matches[0].path;
}

export function staleEnabledTypeIds(localTypeIds, remoteItems) {
  const local = new Set(localTypeIds);
  return remoteItems
    .filter((item) => item.type_id?.S && item.enabled?.BOOL !== false)
    .map((item) => item.type_id.S)
    .filter((typeId) => !local.has(typeId) && !typeId.startsWith("_"))
    .sort();
}

/**
 * `"$presets": ["INTEGRIDAD_PDF"]` dentro de `fields` inyecta los campos de
 * ese preset (`_shared.json` → `_field_presets`) ANTES de los propios del
 * tipo; un campo redeclarado por el tipo gana. Espejo de
 * `document_catalog._parse_field_tree` (Python): el índice de tipos que ve el
 * query Lambda (`document-types.json`) debe mostrar los campos reales, no la
 * referencia. Solo aplica al `fields` de nivel de tipo, no a `item_fields`.
 */
export function expandPresets(fields, fieldPresets, typeId = "", warn = console.warn) {
  if (!fields || typeof fields !== "object" || !("$presets" in fields)) return fields;
  const requested = Array.isArray(fields.$presets) ? fields.$presets : [fields.$presets];
  const expanded = {};
  for (const name of requested) {
    const preset = fieldPresets?.[name];
    if (!preset || typeof preset !== "object") {
      warn(`  ${typeId}: preset de campos desconocido '${name}' (ver _shared.json → _field_presets); ignorado`);
      continue;
    }
    for (const [field, spec] of Object.entries(preset)) {
      if (field.startsWith("__") || !spec || typeof spec !== "object") continue;
      expanded[field] = spec;
    }
  }
  for (const [field, spec] of Object.entries(fields)) {
    if (field === "$presets") continue;
    expanded[field] = spec;
  }
  return expanded;
}
