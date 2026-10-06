#!/usr/bin/env node
/**
 * Pre-deploy safety check: compares the DynamoDB tables in the freshly
 * synthesized template (cdk.out/<stack>.template.json) against the template
 * CloudFormation has deployed, and prints every table the deploy would create,
 * update, replace, or remove — BEFORE the actual deploy runs, so a new table
 * (e.g. CostTable sitting committed but undeployed for weeks, which is exactly
 * what happened before this script existed) is never missed. Wired into
 * `deploy`/`deploy:dev`/`deploy:prod` in package.json, right after the synth.
 *
 * It does NOT run `cdk diff`: that re-synthesizes (and re-bundles every asset)
 * and creates a change set, which doubled the deploy time. One
 * `cloudformation get-template` call is enough to see table changes.
 *
 * dev only reports and lets the deploy continue; prod asks for confirmation
 * whenever any table changes.
 *
 * Usage: node scripts/check-pending-tables.mjs --env dev|prod [--yes]
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { createInterface } from "node:readline/promises";
import { fileURLToPath } from "node:url";
import { diffTables, hasTableChanges } from "./_table-diff.mjs";

function fail(msg) {
  console.error(`❌ ${msg}`);
  process.exit(1);
}

const args = process.argv.slice(2);
const envIdx = args.indexOf("--env");
const environment = (envIdx !== -1 ? args[envIdx + 1] : "dev") === "prod" ? "prod" : "dev";
const yes = args.includes("--yes");
const stackId = environment === "prod" ? "DocumentValidationStack" : "DocumentValidationStack-Dev";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const envFile = path.join(repoRoot, environment === "prod" ? ".env.production" : ".env.dev");

// Same non-overriding .env behavior as bin/app.ts.
if (fs.existsSync(envFile)) {
  for (const line of fs.readFileSync(envFile, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eqIdx = trimmed.indexOf("=");
    if (eqIdx === -1) continue;
    const key = trimmed.slice(0, eqIdx).trim();
    const value = trimmed.slice(eqIdx + 1).trim().replace(/^["']|["']$/g, "");
    if (!process.env[key]) process.env[key] = value;
  }
}
const region = process.env.CDK_DEFAULT_REGION || process.env.AWS_REGION || "us-east-1";

console.log(`🔍 Revisando tablas DynamoDB pendientes en ${stackId} (env=${environment})...`);

const templatePath = path.join(repoRoot, "cdk.out", `${stackId}.template.json`);
if (!fs.existsSync(templatePath)) {
  fail(`No existe ${templatePath}. Corre primero: pnpm run synth:${environment}`);
}
const newTemplate = JSON.parse(fs.readFileSync(templatePath, "utf8"));

let deployedTemplate = null;
try {
  deployedTemplate = JSON.parse(
    execFileSync(
      "aws",
      [
        "cloudformation", "get-template",
        "--stack-name", stackId,
        "--query", "TemplateBody",
        "--output", "json",
        "--region", region,
      ],
      { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], maxBuffer: 1024 * 1024 * 32 }
    )
  );
} catch (e) {
  const stderr = String(e.stderr || e.message);
  // First deploy: no stack yet, so every table in the template is new.
  if (!/does not exist/i.test(stderr)) {
    fail(`No se pudo leer el template desplegado de ${stackId}:\n${stderr}`);
  }
}

const diff = diffTables(deployedTemplate, newTemplate);

if (!hasTableChanges(diff)) {
  console.log("   Sin cambios pendientes en tablas DynamoDB.\n");
  process.exit(0);
}

if (diff.created.length) {
  console.log("\n📦 Tablas NUEVAS que este deploy va a crear:");
  for (const t of diff.created) console.log(`   [+] ${t.logicalId}`);
}
if (diff.updated.length) {
  console.log("\n✏️  Tablas EXISTENTES con cambios in-place:");
  for (const t of diff.updated) console.log(`   [~] ${t.logicalId} (${t.changed.join(", ")})`);
}
if (diff.replaced.length) {
  console.log("\n🛑 Tablas que este deploy va a REEMPLAZAR (tabla nueva vacía; la anterior se borra o queda huérfana):");
  for (const t of diff.replaced) console.log(`   [±] ${t.logicalId} (${t.changed.join(", ")})`);
}
if (diff.removed.length) {
  console.log("\n🛑 Tablas que este deploy va a QUITAR del stack (irreversible salvo RETAIN):");
  for (const t of diff.removed) console.log(`   [-] ${t.logicalId} (DeletionPolicy=${t.deletionPolicy})`);
}
console.log("");

if (environment !== "prod") {
  console.log("   (dev: continuando sin confirmar)\n");
  process.exit(0);
}
if (yes || !process.stdin.isTTY) {
  console.log("   (--yes o sin TTY: continuando sin confirmar)\n");
  process.exit(0);
}

const rl = createInterface({ input: process.stdin, output: process.stdout });
const ans = (await rl.question("¿Continuar con el deploy? (S/n): ")).trim().toLowerCase();
rl.close();
if (ans === "n" || ans === "no") {
  console.log("Cancelado.");
  process.exit(1);
}
