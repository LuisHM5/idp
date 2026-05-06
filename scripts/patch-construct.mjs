#!/usr/bin/env node
/**
 * Patches the default-bedrock-invoke Lambda in the construct to handle
 * markdown-wrapped JSON responses and thinking blocks from newer Claude models.
 */
import { copyFileSync, existsSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import { execSync } from "child_process";

const __dirname = dirname(fileURLToPath(import.meta.url));
const PATCH_SOURCE = resolve(__dirname, "../patches/default-bedrock-invoke-index.py");

// Find the target in node_modules
const targets = [
  resolve(__dirname, "../node_modules/@cdklabs/cdk-appmod-catalog-blueprints/lib/document-processing/resources/default-bedrock-invoke/index.py"),
];

// Also check pnpm nested path
try {
  const pnpmPath = execSync(
    "find node_modules/.pnpm -path '*/cdk-appmod-catalog-blueprints/lib/document-processing/resources/default-bedrock-invoke/index.py' 2>/dev/null",
    { encoding: "utf8", cwd: resolve(__dirname, "..") }
  ).trim();
  if (pnpmPath) {
    for (const p of pnpmPath.split("\n")) {
      targets.push(resolve(__dirname, "..", p.trim()));
    }
  }
} catch {}

let patched = 0;
for (const target of targets) {
  if (existsSync(target)) {
    copyFileSync(PATCH_SOURCE, target);
    patched++;
    console.log(`  ✅ Patched: ${target.split("node_modules/").pop()}`);
  }
}

if (patched === 0) {
  console.warn("  ⚠️  No targets found to patch. Is @cdklabs/cdk-appmod-catalog-blueprints installed?");
} else {
  console.log(`  ✅ Patched ${patched} file(s) - Bedrock response parsing is now robust.`);
}
