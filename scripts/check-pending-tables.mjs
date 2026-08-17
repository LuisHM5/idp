#!/usr/bin/env node
/**
 * Pre-deploy safety check: runs `cdk diff` for the target environment and
 * flags every DynamoDB table it would create, update, or destroy — printed
 * BEFORE the actual deploy runs, so a new table (e.g. CostTable sitting
 * committed but undeployed for weeks, which is exactly what happened before
 * this script existed) is never missed. Wired into
 * `deploy`/`deploy:fast`/`deploy:dev`/`deploy:prod` in package.json.
 *
 * `cdk diff` writes its resource diff to stderr and only exits non-zero when
 * `--fail` is passed (not just because differences exist) — so stderr is
 * redirected into the captured output here, and a thrown execSync error means
 * a real synth/credentials failure, not "there's a diff".
 *
 * Usage: node scripts/check-pending-tables.mjs --env dev|prod [--yes]
 */
import { execSync } from "child_process";
import { createInterface } from "node:readline/promises";

function fail(msg) {
  console.error(`❌ ${msg}`);
  process.exit(1);
}

const args = process.argv.slice(2);
const envIdx = args.indexOf("--env");
const environment = (envIdx !== -1 ? args[envIdx + 1] : "dev") === "prod" ? "prod" : "dev";
const yes = args.includes("--yes");
const stackId = environment === "prod" ? "DocumentValidationStack" : "DocumentValidationStack-Dev";

console.log(`🔍 Revisando tablas DynamoDB pendientes en ${stackId} (env=${environment})...`);

let diffOutput;
try {
  diffOutput = execSync(`npx cdk diff ${stackId} -c env=${environment} 2>&1`, {
    encoding: "utf8",
    maxBuffer: 1024 * 1024 * 32,
  });
} catch (e) {
  fail(
    `cdk diff falló (no es solo "hay diferencias" — eso no da exit code distinto de 0 sin --fail):\n${e.stdout || e.message}`
  );
}

const tableLines = diffOutput
  .split("\n")
  .map((l) => l.trim())
  .filter((l) => /^\[[+~-]\]\s+AWS::DynamoDB::Table\b/.test(l));

if (tableLines.length === 0) {
  console.log("   Sin cambios pendientes en tablas DynamoDB.\n");
  process.exit(0);
}

const created = tableLines.filter((l) => l.startsWith("[+]"));
const updated = tableLines.filter((l) => l.startsWith("[~]"));
const destroyed = tableLines.filter((l) => l.startsWith("[-]"));

if (created.length) {
  console.log("\n📦 Tablas NUEVAS que este deploy va a crear:");
  for (const l of created) console.log(`   ${l}`);
}
if (updated.length) {
  console.log("\n✏️  Tablas EXISTENTES con cambios (revisa que no sea disruptivo, ej. encriptación/PK):");
  for (const l of updated) console.log(`   ${l}`);
}
if (destroyed.length) {
  console.log("\n🛑 Tablas que este deploy va a BORRAR (irreversible salvo RETAIN):");
  for (const l of destroyed) console.log(`   ${l}`);
}
console.log("");

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
