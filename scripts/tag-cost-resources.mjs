#!/usr/bin/env node
/**
 * Ensures and tags resources that are intentionally imported by name and
 * therefore cannot be created/tagged through the application stack.
 *
 * Usage: node scripts/tag-cost-resources.mjs --env dev|prod
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
const requestedEnvironment = envIdx === -1 ? undefined : args[envIdx + 1];
if (!requestedEnvironment || !["dev", "prod"].includes(requestedEnvironment)) {
  fail("Uso: node scripts/tag-cost-resources.mjs --env dev|prod");
}

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const envFile = path.join(
  repoRoot,
  requestedEnvironment === "prod" ? ".env.production" : ".env.dev"
);

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

const stackName =
  requestedEnvironment === "prod"
    ? "DocumentValidationStack"
    : "DocumentValidationStack-Dev";
const defaultTableName =
  requestedEnvironment === "prod"
    ? "document-validation-catalog"
    : "document-validation-catalog-dev";
const tableName = process.env.CATALOG_TABLE || defaultTableName;
const region = process.env.CDK_DEFAULT_REGION || process.env.AWS_REGION || "us-east-1";

function aws(...commandArgs) {
  return execFileSync("aws", [...commandArgs, "--region", region], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
}

let tableArn;
try {
  tableArn = aws(
    "dynamodb",
    "describe-table",
    "--table-name",
    tableName,
    "--query",
    "Table.TableArn",
    "--output",
    "text"
  );
} catch (error) {
  const detail = error.stderr?.trim() || error.message;
  if (!detail.includes("ResourceNotFoundException")) {
    fail(`No se pudo resolver la tabla ${tableName} en ${region}: ${detail}`);
  }
}

if (!tableArn) {
  console.log(`📦 Creando ${tableName} en ${region}...`);
  try {
    aws(
      "dynamodb",
      "create-table",
      "--table-name",
      tableName,
      "--attribute-definitions",
      "AttributeName=type_id,AttributeType=S",
      "--key-schema",
      "AttributeName=type_id,KeyType=HASH",
      "--billing-mode",
      "PAY_PER_REQUEST",
      "--tags",
      `Key=CostStack,Value=${stackName}`
    );
    aws("dynamodb", "wait", "table-exists", "--table-name", tableName);
    tableArn = aws(
      "dynamodb",
      "describe-table",
      "--table-name",
      tableName,
      "--query",
      "Table.TableArn",
      "--output",
      "text"
    );
  } catch (error) {
    fail(
      `No se pudo crear ${tableName}: ${error.stderr?.trim() || error.message}`
    );
  }
}

try {
  aws(
    "dynamodb",
    "tag-resource",
    "--resource-arn",
    tableArn,
    "--tags",
    `Key=CostStack,Value=${stackName}`
  );
} catch (error) {
  fail(
    `No se pudo etiquetar ${tableName}: ${error.stderr?.trim() || error.message}`
  );
}

// DynamoDB tag reads are eventually consistent. Retry the read so a successful
// TagResource is not reported as a failure merely because ListTags still sees
// the previous state for a few seconds.
let appliedTag;
for (let attempt = 1; attempt <= 10; attempt += 1) {
  appliedTag = aws(
    "dynamodb",
    "list-tags-of-resource",
    "--resource-arn",
    tableArn,
    "--query",
    "Tags[?Key=='CostStack']|[0].Value",
    "--output",
    "text"
  );
  if (appliedTag === stackName) break;
  await new Promise((resolve) => setTimeout(resolve, 1500));
}

if (appliedTag !== stackName) {
  fail(`AWS no devolvió la etiqueta esperada para ${tableName}`);
}

console.log(`✅ ${tableName}: CostStack=${stackName}`);
