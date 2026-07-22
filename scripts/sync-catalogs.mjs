#!/usr/bin/env node
/**
 * Sync catalog sources into the enrichment Lambda bundle.
 *
 * `catalogs/full-pedimentos-docs.json` is the source of truth for the customs
 * (pedimentos) catalog; the Lambda only packages its own config/ directory,
 * so this script copies it to `config/customs_catalog.json`. Runs as part of
 * `pnpm build`.
 */

import { copyFileSync } from "fs";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repo = resolve(__dirname, "..");

const pairs = [
  [
    resolve(repo, "catalogs/full-pedimentos-docs.json"),
    resolve(
      repo,
      "src/document-processing/resources/enrichment-lambda/config/customs_catalog.json"
    ),
  ],
];

for (const [src, dst] of pairs) {
  copyFileSync(src, dst);
  console.log(`synced ${src} -> ${dst}`);
}
