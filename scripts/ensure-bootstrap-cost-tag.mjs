#!/usr/bin/env node
/**
 * Ensures the CDK bootstrap stack exists and carries its cost-allocation tag.
 * A normal deploy only performs a read; bootstrapping runs when the stack is
 * missing or the expected tag is absent.
 *
 * Usage: node scripts/ensure-bootstrap-cost-tag.mjs --env dev|prod
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

function fail(message) {
  console.error(`❌ ${message}`);
  process.exit(1);
}

const args = process.argv.slice(2);
const envIdx = args.indexOf("--env");
const environment = envIdx === -1 ? undefined : args[envIdx + 1];
if (!environment || !["dev", "prod"].includes(environment)) {
  fail("Uso: node scripts/ensure-bootstrap-cost-tag.mjs --env dev|prod");
}

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const envFile = path.join(
  repoRoot,
  environment === "prod" ? ".env.production" : ".env.dev"
);
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
let currentTag;
try {
  currentTag = execFileSync(
    "aws",
    [
      "cloudformation",
      "describe-stacks",
      "--stack-name",
      "CDKToolkit",
      "--query",
      "Stacks[0].Tags[?Key=='CostStack']|[0].Value",
      "--output",
      "text",
      "--region",
      region,
    ],
    { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }
  ).trim();
} catch (error) {
  const detail = error.stderr?.trim() || error.message;
  if (!detail.includes("does not exist")) {
    fail(`No se pudo revisar CDKToolkit en ${region}: ${detail}`);
  }
}

if (currentTag === "CDKToolkit") {
  console.log("✅ CDKToolkit: CostStack=CDKToolkit");
  process.exit(0);
}

console.log("📦 Preparando CDKToolkit con CostStack=CDKToolkit...");
try {
  execFileSync(
    "npx",
    [
      "cdk",
      "bootstrap",
      "-c",
      `env=${environment}`,
      "--tags",
      "CostStack=CDKToolkit",
    ],
    { cwd: repoRoot, stdio: "inherit" }
  );
} catch (error) {
  fail(`No se pudo preparar CDKToolkit: ${error.message}`);
}
