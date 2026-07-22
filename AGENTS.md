# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project Overview

**Documents Validator** is an extensible Mexican document validation and extraction system built on AWS. It combines Amazon Bedrock (Claude) for classification and field extraction with a Python Lambda that applies deterministic validation rules defined in a JSON catalog (optionally hot-reloadable via DynamoDB).

Supported document types out of the box: INE, Pasaporte, Constancia de Situación Fiscal (CSF), Carta NOM-35, CURP, and custom internal formats.

## Architecture

```
Upload API → S3 (raw/) → Bedrock Classification → Bedrock Extraction
           → Enrichment Lambda (deterministic + custom validators)
           → EventBridge (APROBADO / RECHAZADO / REQUIERE_REVISION)
```

Key pieces:

- `bin/app.ts` — CDK entry point. Loads `.env.production` and instantiates the stack.
- `src/document-validation-stack.ts` — Main CDK stack. Wires Upload Lambda, Bedrock pipeline (via `@cdklabs/cdk-appmod-catalog-blueprints`), Enrichment Lambda, optional DynamoDB catalog, and EventBridge broker.
- `src/prompt-generator.ts` / `src/document-catalog.ts` — Generate Bedrock classification and extraction prompts from the JSON catalog at synth time, injecting `extraction_hints` so the model knows where to look for each field.
- `src/document-processing/resources/enrichment-lambda/` — Python 3.12 validation engine (do not modify `handler.py` lightly; extend via config and validators instead).
  - `config/document_types.json` — **The catalog. Primary extension point.**
  - `config/customs_catalog.json` — Customs/pedimentos catalog, **generated copy** of `catalogs/full-pedimentos-docs.json` (synced by `pnpm build` / `pnpm sync-catalogs`; edit the source, not the copy).
  - `validators/` — Built-in rule modules plus `engine.py`, the dynamic rule engine (formulas, sum_array, cross-document, per-field statuses). `validators/customs/*` are thin shims — customs rules live entirely in the catalog JSON.
  - `pipeline/` — Expediente/pedimento pipeline: `pdf_pages` (pypdf layout text + Textract per-page OCR fallback), `segmenter` (Bedrock page-range classification with deterministic markers), `text_extractor` (catalog-schema extraction incl. repeatable fields, chunked), `expediente` (orchestrator + cross-document checks), `normalize` (deterministic amounts/dates/pedimento/UUID normalization + regex reference recovery).
  - `custom_validators/*.mjs` — Optional Node.js custom validators for logic beyond regex.
- `tests/unit/` — Offline pytest suite for the rule engine and normalizers (`pnpm test:unit`).
- `scripts/test-pedimentos.py` + `tests/integration/expected_pedimentos.json` — Integration runner with ground truth for the PDFs in `test-docs/pedimentos docs/docs` (`pnpm test:pedimentos`; needs AWS creds, costs ~$1/run).
- `src/document-processing/resources/upload-lambda/index.mjs` — Node.js 20 REST upload endpoint.
- `catalogs/` — Per-type JSON files (one document type per file, keyed by `type_id`) synced to DynamoDB via `pnpm db-sync`. Use this for hot-reload changes without touching the bundled JSON or redeploying. Root shape is `{ "TYPE_ID": { ...entry } }` — no `types:` wrapper.
- `scripts/` — CLI tooling (add types, preview prompts, DynamoDB hot-reload, end-to-end tests).
- `patches/default-bedrock-invoke-index.py` — Patch applied by `scripts/patch-construct.mjs` during `pnpm build`.

## Environment

- Node.js 20+, pnpm 10+ (package manager pinned: `pnpm@10.20.0`).
- AWS CDK v2 (`aws-cdk-lib ^2.252.0`), deployed via `npx cdk`.
- Python 3.12 for the enrichment Lambda.
- `.env.production` (copied from `.env.example`) holds `BEDROCK_MODEL_ID`, `CDK_DEFAULT_ACCOUNT`, `CDK_DEFAULT_REGION`, `CATALOG_TABLE`, `AWS_REGION`.

## Commands

Run everything with pnpm from the repo root.

| Command | Purpose |
|---|---|
| `pnpm build` | Run `scripts/patch-construct.mjs` then `tsc`. Run this before `deploy` / `diff` / `synth`. |
| `pnpm synth` | Produce CloudFormation via `npx cdk synth`. |
| `pnpm run diff` | Show pending changes vs deployed stack. |
| `pnpm run deploy` | Deploy (`--require-approval never`). |
| `pnpm run destroy` / `pnpm run destroy:full` | Tear down stack (full variant cleans lingering resources). |
| `pnpm list-types` | List types from the local JSON catalog. |
| `pnpm preview-prompts` | Render the actual prompts Bedrock will receive. Always run this after catalog edits. |
| `pnpm add-type` | Scaffold a new type inside `document_types.json`. |
| `pnpm add-validator` | Scaffold a custom Node.js validator under `custom_validators/`. |
| `pnpm db-put-type` / `pnpm db-list-types` / `pnpm db-sync` | Manage the DynamoDB catalog for hot-reload without redeploy. `db-sync` accepts the customs catalog shape (`document_types` + `_field_sets`, resolved before upload) and `--dry-run`. |
| `pnpm db-sync-customs` | Push `catalogs/full-pedimentos-docs.json` to DynamoDB (hot-reload). |
| `pnpm db-delete-type` | Delete or disable (`--disable`) a type in DynamoDB; `--all` empties the table. |
| `pnpm sync-catalogs` / `pnpm setup:python` | Copy catalog sources into the Lambda config / create the test venv. |
| `pnpm test-local` / `pnpm test-e2e` | Local handler tests and end-to-end pipeline tests. |
| `pnpm track` | Track a Step Functions execution. |

Python tests use pytest from the project venv (`.venv`): `pnpm test:unit` is offline; `pnpm test:pedimentos` hits real AWS (Textract + Bedrock).

## Catalog Loading Precedence

When the enrichment Lambda runs, it loads types in this order (last wins):

1. `config/document_types.json` — shipped with the Lambda code.
2. DynamoDB (`CATALOG_TABLE` env var) — overrides JSON entries when `enableDynamoCatalog: true`.

`forceReloadCatalog: true` reloads on every invocation (small perf cost, immediate DynamoDB effect).

## How To Extend

Pick the lowest-impact path that solves the request:

1. **New document type, standard rules** → preferred flow: drop a new file under `catalogs/<name>.json` (single-type object) and run `pnpm db-sync --file catalogs/<name>.json` for immediate effect (no redeploy). Alternatively, edit `config/document_types.json` (or `pnpm add-type`) and `pnpm build && pnpm run deploy` to bake it into the Lambda. `pnpm db-sync --all` syncs every type from the local config. Re-syncing a type overwrites the DynamoDB row entirely.
2. **New extraction field or hint** → add it to `extraction_fields` / `extraction_hints` in the catalog. Hints are free-form Spanish text describing *where* on the document the field lives.
3. **New rule type beyond regex / date_expiry / presence / boolean_true** → extend `validators/base.py` and the corresponding module under `validators/`.
4. **External API calls, cross-field checks, check digits** → create a `custom_validators/<name>.mjs` exporting `async function validate(entityMap, context)` that returns `[{ field, rule, valid, message }]`. Reference it via `custom_validator` in the catalog entry.

Avoid editing `handler.py` for feature work. It is the engine; features live in the catalog and validators.

## Conventions

- TypeScript strict-ish CDK code, 2-space indent, double quotes, semicolons (see `src/document-validation-stack.ts`).
- Python is type-hinted and docstring-heavy (see `handler.py`). Keep it that way.
- Document IDs, field names, and classification labels in catalog entries are UPPER_SNAKE_CASE and Spanish (`NOMBRE_COMPLETO`, `CURP`, `CONSTANCIA_SITUACION_FISCAL`).
- User-facing messages and docs in the existing codebase are Spanish; match that when editing existing files. New files may be English if the user asks.
- Canonical validation status values: `APROBADO`, `RECHAZADO`, `REQUIERE_REVISION`. Do not invent new ones.
- Do not commit `.env.production` or anything under `cdk.out/`, `dist/`, `node_modules/`.

## Safety Notes For Agents

- This stack touches AWS (Bedrock, Lambda, API Gateway, DynamoDB, EventBridge, S3). Treat `pnpm run deploy`, `pnpm run destroy`, and `db-put-type` against a real account as high-risk and confirm with the user before running.
- The DynamoDB catalog table has `RETAIN` semantics (imported via `fromTableName`). It is not destroyed with the stack; do not assume `destroy` removes it.
- `iam-policy.json` defines the broad Bedrock invoke permissions the pipeline needs. Do not narrow it without verifying inference-profile ARNs still resolve.
- Test documents under `test-docs/` and events under `test-events/` are for local testing only; keep any PII out of commits.

## Validation Before Handing Off

After changes, at minimum:

1. `pnpm build` to ensure TypeScript and the patch script succeed.
2. If the catalog changed, `pnpm preview-prompts` to confirm prompts still render correctly.
3. If Python code changed, run `pnpm test-local` when feasible.
4. Mention any deploy or `db-put-type` steps the user still needs to execute.
