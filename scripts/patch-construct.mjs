#!/usr/bin/env node
/**
 * Applies the project-owned compatibility patches required on
 * @cdklabs/cdk-appmod-catalog-blueprints without forking the whole package:
 *
 * - configurable encryption: AWS-owned/default encryption with zero CMKs in
 *   dev; one caller-supplied key reused everywhere in prod. The upstream 1.17
 *   construct otherwise creates customer-managed keys unconditionally.
 *
 * - default-sqs-consumer: derives `documentId` from the uuid segment right
 *   before the filename instead of blindly stripping the `raw/` prefix, so
 *   manually classified uploads (raw/{TYPE}/{uuid}/{file}) keep the same
 *   documentId shape as auto-classified ones (raw/{uuid}/{file}) — otherwise
 *   GET /documents/{id}'s prefix lookup can never find them (documentId came
 *   out prefixed by TYPE instead of by the uuid the Upload API returned).
 *
 * The model-invoking Lambda used to be patched here too. It is now our own
 * function, wired in through the construct's documented extension points —
 * see `src/document-processing/multi-model-document-processing.ts`. Overwriting
 * a file inside node_modules only held until the next dependency bump.
 */
import { copyFileSync, existsSync, realpathSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import { execSync } from "child_process";
import { patchBlueprintEncryption } from "../patches/configurable-encryption.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));

const packageRoots = new Set();
const directPackageRoot = resolve(
  __dirname,
  "../node_modules/@cdklabs/cdk-appmod-catalog-blueprints"
);
if (existsSync(directPackageRoot)) packageRoots.add(realpathSync(directPackageRoot));
try {
  const nestedPackageJson = execSync(
    "find node_modules/.pnpm -path '*/node_modules/@cdklabs/cdk-appmod-catalog-blueprints/package.json' 2>/dev/null",
    { encoding: "utf8", cwd: resolve(__dirname, "..") }
  ).trim();
  for (const packageJson of nestedPackageJson ? nestedPackageJson.split("\n") : []) {
    packageRoots.add(realpathSync(dirname(resolve(__dirname, "..", packageJson.trim()))));
  }
} catch {}

if (packageRoots.size === 0) {
  throw new Error("@cdklabs/cdk-appmod-catalog-blueprints is not installed");
}
for (const packageRoot of packageRoots) {
  patchBlueprintEncryption(packageRoot);
  console.log(`  ✅ Patched configurable encryption: ${packageRoot.split("node_modules/").pop()}`);
}

const PATCHES = [
  {
    source: resolve(__dirname, "../patches/default-sqs-consumer-index.py"),
    relTarget: "lib/document-processing/resources/default-sqs-consumer/index.py",
    label: "SQS consumer documentId derivation",
  },
];

let totalPatched = 0;
for (const { source, relTarget, label } of PATCHES) {
  // Find the target(s) in node_modules
  const targets = new Set();
  const directTarget = resolve(
    __dirname,
    "../node_modules/@cdklabs/cdk-appmod-catalog-blueprints",
    relTarget
  );
  if (existsSync(directTarget)) targets.add(realpathSync(directTarget));

  // Also check pnpm nested path
  try {
    const pnpmPath = execSync(
      `find node_modules/.pnpm -path '*/cdk-appmod-catalog-blueprints/${relTarget}' 2>/dev/null`,
      { encoding: "utf8", cwd: resolve(__dirname, "..") }
    ).trim();
    if (pnpmPath) {
      for (const p of pnpmPath.split("\n")) {
        const nestedTarget = resolve(__dirname, "..", p.trim());
        if (existsSync(nestedTarget)) targets.add(realpathSync(nestedTarget));
      }
    }
  } catch {}

  let patched = 0;
  for (const target of targets) {
    if (existsSync(target)) {
      copyFileSync(source, target);
      patched++;
      console.log(`  ✅ Patched: ${target.split("node_modules/").pop()}`);
    }
  }

  if (patched === 0) {
    console.warn(`  ⚠️  No targets found for ${relTarget}. Is @cdklabs/cdk-appmod-catalog-blueprints installed?`);
  } else {
    console.log(`  ✅ Patched ${patched} file(s) - ${label} is now fixed.`);
  }
  totalPatched += patched;
}

if (totalPatched === 0) {
  console.warn("  ⚠️  No targets found to patch. Is @cdklabs/cdk-appmod-catalog-blueprints installed?");
}
