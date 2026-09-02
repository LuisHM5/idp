#!/usr/bin/env node
/**
 * Sync catalog types to DynamoDB (hot-reload, no redeploy).
 *
 * ONE source of truth: the `catalogs/` folder — one JSON per type,
 * shape { "<TYPE_ID>": { ...entry } }, plus catalogs/_shared.json
 * (holds _field_sets, resolved into each type before upload).
 *
 * Two commands (see package.json):
 *   pnpm sync                      → sync ALL types in catalogs/ to DynamoDB
 *   pnpm sync:one -- <selector>    → sync ONE type
 *
 * Environment: dev and prod have SEPARATE catalog tables
 * (document-validation-catalog-dev / document-validation-catalog). Like
 * `cdk deploy`, the default here is DEV, so a bare `pnpm sync` never touches
 * prod; use `pnpm sync:prod` (--env prod) for production. The table name comes
 * from CATALOG_TABLE in .env.dev / .env.production.
 *       <selector> can be:
 *         --file catalogs/PEDIMENTO_ADUANAL.json   (exact path)
 *         --type PEDIMENTO_ADUANAL                 (catalogs/<TYPE>.json)
 *         PEDIMENTO                                (name/substring, case-insensitive)
 *
 * Options:
 *   --dry-run   Show what would be uploaded without writing
 *   --env       dev | prod (default: dev) — carga .env.dev / .env.production
 *   --table     DynamoDB table (default: CATALOG_TABLE del entorno)
 *   --region    AWS region (default: AWS_REGION env or "us-east-1")
 */

import { readFileSync, writeFileSync, readdirSync, existsSync, unlinkSync } from "fs";
import { resolve, dirname, join, basename } from "path";
import { fileURLToPath } from "url";
import { execSync } from "child_process";
import { uniqueKeysOf } from "./_unique_keys.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CATALOGS_DIR = resolve(__dirname, "../catalogs");

const BOOL_FLAGS = new Set(["dry-run", "one"]);

function parseArgs(argv) {
  const opts = {};
  const positional = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--") continue;
    if (a.startsWith("--")) {
      const key = a.slice(2);
      if (BOOL_FLAGS.has(key)) {
        opts[key] = true;
      } else if (i + 1 < argv.length && !argv[i + 1].startsWith("--")) {
        opts[key] = argv[++i];
      } else {
        opts[key] = true;
      }
    } else {
      positional.push(a);
    }
  }
  return { opts, positional };
}

/** Generic JS → DynamoDB AttributeValue marshaller (preserves every key). */
function toAttr(value) {
  if (value === null || value === undefined) return { NULL: true };
  if (typeof value === "boolean") return { BOOL: value };
  if (typeof value === "number") return { N: String(value) };
  if (typeof value === "string") return { S: value };
  if (Array.isArray(value)) return { L: value.map(toAttr) };
  if (typeof value === "object") {
    const m = {};
    for (const [k, v] of Object.entries(value)) m[k] = toAttr(v);
    return { M: m };
  }
  return { S: String(value) };
}

/** Expand `field_sets` references into extraction_fields (DynamoDB has no _field_sets). */
function resolveFieldSets(config, fieldSets) {
  if (!config.field_sets || !fieldSets) return config;
  const fields = [];
  for (const setName of config.field_sets) {
    for (const f of fieldSets[setName] || []) if (!fields.includes(f)) fields.push(f);
  }
  for (const f of config.extraction_fields || []) if (!fields.includes(f)) fields.push(f);
  const resolved = { ...config, extraction_fields: fields };
  delete resolved.field_sets;
  return resolved;
}

/** All per-type files in catalogs/ — any "_*.json" file is special (not a
 * document type): _shared.json, _TEMPLATE.json, and every per-source-format
 * template (_TEMPLATE_XML.json, _TEMPLATE_TABULAR.json, ...). Filtering by
 * prefix instead of an explicit filename allowlist means a new template
 * never needs a code change here. */
function typeFiles() {
  return readdirSync(CATALOGS_DIR)
    .filter((f) => f.endsWith(".json") && !f.startsWith("_"))
    .sort();
}

/** Resolve `pnpm sync:one` selector to a single file path. */
function selectFile({ opts, positional }) {
  if (opts.file) {
    const p = resolve(opts.file);
    if (!existsSync(p)) throw new Error(`archivo no encontrado: ${opts.file}`);
    return p;
  }
  // --type is an exact TYPE_ID; a positional name/--name is a fuzzy substring.
  if (opts.type) {
    const p = join(CATALOGS_DIR, `${opts.type}.json`);
    if (!existsSync(p)) throw new Error(`tipo no encontrado: catalogs/${opts.type}.json`);
    return p;
  }
  const files = typeFiles();
  const needle = (opts.name || positional[0] || "").toLowerCase();
  if (!needle) throw new Error("indica un tipo: --type X, --file ruta, o un nombre");
  const matches = files.filter((f) => basename(f, ".json").toLowerCase().includes(needle));
  if (matches.length === 0) throw new Error(`ningún tipo coincide con '${needle}'`);
  if (matches.length > 1) {
    throw new Error(`'${needle}' es ambiguo: ${matches.join(", ")} — usa --type o --file`);
  }
  return join(CATALOGS_DIR, matches[0]);
}

function putType(typeId, config, table, region, dryRun) {
  const item = { type_id: { S: typeId }, enabled: { BOOL: true } };
  for (const [k, v] of Object.entries(config)) {
    if (k === "type_id" || k === "enabled") continue;
    item[k] = toAttr(v);
  }
  // Atributo DERIVADO (no viene del JSON tal cual): las unique keys del tipo,
  // ya resueltas a [{field, normalize}] desde `unique_key` de nivel de tipo y
  // desde cada `fields[X].unique_key`. El query Lambda proyecta solo este
  // atributo para saber qué campo se busca por `TIPODOC#valor`; leer el mapa
  // `fields` completo para eso costaría órdenes de magnitud más. Se
  // sobrescribe siempre (incluso vacío) para que quitar una unique key del
  // catálogo y hacer `pnpm sync:one` la quite de verdad, sin dejar la anterior
  // viva en la fila hot-reload.
  item.unique_keys = toAttr(uniqueKeysOf(config));
  const itemJson = JSON.stringify(item);
  if (dryRun) {
    console.log(`  🔎 [dry-run] ${typeId}: ${Object.keys(item).length} atributos, ${itemJson.length} bytes`);
    return true;
  }
  const tmp = `/tmp/ddb-item-${typeId}-${Date.now()}.json`;
  writeFileSync(tmp, itemJson);
  try {
    execSync(`aws dynamodb put-item --table-name "${table}" --item file://${tmp} --region ${region}`,
      { stdio: "pipe", timeout: 15000 });
    unlinkSync(tmp);
    return true;
  } catch (e) {
    try { unlinkSync(tmp); } catch {}
    console.error(`  ❌ ${typeId}: ${e.stderr?.toString().trim() || e.message}`);
    return false;
  }
}

/** Carga .env.dev / .env.production SIN pisar variables ya exportadas —
 * mismo comportamiento que bin/app.ts, para que el nombre de tabla salga de
 * una sola fuente (CATALOG_TABLE) en los dos caminos: deploy y sync. */
function loadEnvFile(environment) {
  const file = resolve(
    __dirname,
    environment === "prod" ? "../.env.production" : "../.env.dev"
  );
  if (!existsSync(file)) return;
  for (const line of readFileSync(file, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    const value = trimmed.slice(eq + 1).trim().replace(/^["']|["']$/g, "");
    if (!process.env[key]) process.env[key] = value;
  }
}

function main() {
  const { opts, positional } = parseArgs(process.argv.slice(2));
  // Default dev (igual que `cdk deploy`): un `pnpm sync` a secas nunca escribe
  // en el catálogo de producción.
  const environment = String(opts.env || "dev") === "prod" ? "prod" : "dev";
  loadEnvFile(environment);
  const table =
    opts.table ||
    process.env.CATALOG_TABLE ||
    `document-validation-catalog${environment === "prod" ? "" : "-dev"}`;
  const region = opts.region || process.env.AWS_REGION || "us-east-1";
  const dryRun = !!opts["dry-run"];
  const one = !!(opts.one || opts.file || opts.type || opts.name || positional.length);

  const shared = existsSync(join(CATALOGS_DIR, "_shared.json"))
    ? JSON.parse(readFileSync(join(CATALOGS_DIR, "_shared.json"), "utf8"))
    : {};
  const fieldSets = shared._field_sets || {};

  let files;
  if (one) {
    files = [selectFile({ opts, positional })];
  } else {
    files = typeFiles().map((f) => join(CATALOGS_DIR, f));
  }

  console.log(`\n📤 Sincronizando ${files.length} tipo(s) a DynamoDB [env=${environment}] (${table} @ ${region})${dryRun ? " [dry-run]" : ""}...\n`);

  let ok = 0, fail = 0;
  for (const file of files) {
    const data = JSON.parse(readFileSync(file, "utf8"));
    for (const [typeId, cfg] of Object.entries(data)) {
      if (typeId.startsWith("_")) continue;
      const resolved = resolveFieldSets(cfg, fieldSets);
      if (putType(typeId, resolved, table, region, dryRun)) {
        console.log(`  ✅ ${typeId}`);
        ok++;
      } else {
        fail++;
      }
    }
  }

  console.log(`\n   Resultado: ${ok} ok, ${fail} fallido(s)`);
  if (ok > 0 && !dryRun) console.log(`   ⚡ Efecto inmediato — no requiere redeploy.\n`);
  if (fail > 0) process.exit(1);
}

main();
