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
 *         --file catalogs/base/aduanas/PEDIMENTO_ADUANAL.json (exact path)
 *         --type PEDIMENTO_ADUANAL                 (recursive TYPE_ID lookup)
 *         PEDIMENTO                                (name/substring, case-insensitive)
 *
 * Options:
 *   --dry-run   Show what would be uploaded without writing
 *   --prune     Mark enabled=false remote types absent from the active tree
 *   --env       dev | prod (default: dev) — carga .env.dev / .env.production
 *   --table     DynamoDB table (default: CATALOG_TABLE del entorno)
 *   --region    AWS region (default: AWS_REGION env or "us-east-1")
 */

import { readFileSync, writeFileSync, existsSync, unlinkSync } from "fs";
import { resolve, dirname, join } from "path";
import { fileURLToPath } from "url";
import { execFileSync } from "child_process";
import { uniqueKeysOf } from "./_unique_keys.mjs";
import {
  discoverCatalogTypes,
  readCatalogTypeFile,
  selectCatalogFile,
  staleEnabledTypeIds,
} from "./catalog-files.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CATALOGS_DIR = resolve(__dirname, "../catalogs");

const BOOL_FLAGS = new Set(["dry-run", "one", "prune"]);

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

/** Resolve `pnpm sync:one` selector to a single file path. */
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
    execFileSync(
      "aws",
      ["dynamodb", "put-item", "--table-name", table, "--item", `file://${tmp}`, "--region", region],
      { stdio: "pipe", timeout: 15000 }
    );
    unlinkSync(tmp);
    return true;
  } catch (e) {
    try { unlinkSync(tmp); } catch {}
    console.error(`  ❌ ${typeId}: ${e.stderr?.toString().trim() || e.message}`);
    return false;
  }
}

function remoteCatalogItems(table, region) {
  const output = execFileSync(
    "aws",
    [
      "dynamodb", "scan",
      "--table-name", table,
      "--projection-expression", "#type, #enabled",
      "--expression-attribute-names", JSON.stringify({ "#type": "type_id", "#enabled": "enabled" }),
      "--region", region,
      "--output", "json",
    ],
    { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 30000 }
  );
  return JSON.parse(output).Items || [];
}

function disableRemoteType(typeId, table, region, dryRun) {
  if (dryRun) {
    console.log(`  🔎 [dry-run] deshabilitar ${typeId}`);
    return true;
  }
  try {
    execFileSync(
      "aws",
      [
        "dynamodb", "update-item",
        "--table-name", table,
        "--key", JSON.stringify({ type_id: { S: typeId } }),
        "--update-expression", "SET enabled = :disabled",
        "--expression-attribute-values", JSON.stringify({ ":disabled": { BOOL: false } }),
        "--region", region,
      ],
      { stdio: "pipe", timeout: 15000 }
    );
    console.log(`  📴 ${typeId}`);
    return true;
  } catch (e) {
    console.error(`  ❌ no se pudo deshabilitar ${typeId}: ${e.stderr?.toString().trim() || e.message}`);
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
  const prune = !!opts.prune;
  const one = !!(opts.one || opts.file || opts.type || opts.name || positional.length);

  const shared = existsSync(join(CATALOGS_DIR, "_shared.json"))
    ? JSON.parse(readFileSync(join(CATALOGS_DIR, "_shared.json"), "utf8"))
    : {};
  const fieldSets = shared._field_sets || {};

  const discovered = discoverCatalogTypes(CATALOGS_DIR);
  let files;
  if (one) {
    files = [selectCatalogFile({ opts, positional }, discovered, CATALOGS_DIR)];
  } else {
    files = discovered.map(({ path }) => path);
  }

  console.log(`\n📤 Sincronizando ${files.length} tipo(s) a DynamoDB [env=${environment}] (${table} @ ${region})${dryRun ? " [dry-run]" : ""}...\n`);

  let ok = 0, fail = 0;
  for (const file of files) {
    const { typeId, entry } = readCatalogTypeFile(file, CATALOGS_DIR);
    const resolved = resolveFieldSets(entry, fieldSets);
    if (putType(typeId, resolved, table, region, dryRun)) {
      console.log(`  ✅ ${typeId}`);
      ok++;
    } else {
      fail++;
    }
  }

  if (!one && fail === 0) {
    try {
      const stale = staleEnabledTypeIds(
        discovered.map(({ typeId }) => typeId),
        remoteCatalogItems(table, region)
      );
      if (stale.length) {
        console.log(`\n   Tipos habilitados solo en DynamoDB: ${stale.join(", ")}`);
        if (prune) {
          for (const typeId of stale) {
            if (!disableRemoteType(typeId, table, region, dryRun)) fail++;
          }
        } else {
          console.log("   Usa --prune para marcarlos enabled=false (no se borran).");
        }
      }
    } catch (e) {
      const message = e.stderr?.toString().trim() || e.message;
      if (prune) {
        console.error(`\n  ❌ no se pudo auditar DynamoDB para --prune: ${message}`);
        fail++;
      } else {
        console.warn(`\n  ⚠ no se pudieron auditar tipos obsoletos en DynamoDB: ${message}`);
      }
    }
  }

  console.log(`\n   Resultado: ${ok} ok, ${fail} fallido(s)`);
  if (ok > 0 && !dryRun) console.log(`   ⚡ Efecto inmediato — no requiere redeploy.\n`);
  if (fail > 0) process.exit(1);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
