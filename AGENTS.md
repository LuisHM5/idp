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
  - `config/catalogs.json` — **Bundled catalog. GENERATED — do not edit.** Produced by `pnpm build` (scripts/sync-catalogs.mjs) from the `catalogs/` folder. Edit the source in `catalogs/`, never this file.
  - `validators/` — Built-in rule modules plus `engine.py`, the dynamic rule engine (formulas, sum_array, cross-document, per-field statuses). `validators/customs/*` are thin shims — customs rules live entirely in the catalog JSON.
  - `pipeline/` — Expediente/pedimento pipeline: `pdf_pages` (pypdf layout text + Textract per-page OCR fallback), `segmenter` (Bedrock page-range classification with deterministic markers), `text_extractor` (catalog-schema extraction incl. repeatable fields, chunked), `expediente` (orchestrator + cross-document checks), `normalize` (deterministic amounts/dates/pedimento/UUID normalization + regex reference recovery).
  - `custom_validators/*.mjs` — Optional Node.js custom validators for logic beyond regex.
- `tests/unit/` — Offline pytest suite for the rule engine and normalizers (`pnpm test:unit`).
- `scripts/test-pedimentos.py` + `tests/integration/expected_pedimentos.json` — Integration runner with ground truth for the PDFs in `test-docs/pedimentos docs/docs` (`pnpm test:pedimentos`; needs AWS creds, costs ~$1/run).
- `src/document-processing/resources/upload-lambda/index.mjs` — Node.js 20 REST upload endpoint (`POST /upload`).
- `src/document-processing/resources/query-lambda/index.mjs` — Node.js 20 REST read/search endpoints (`GET /documents/{id}` by id; `GET /documents?docType=` filters by type via **Query on the `DocumentTypeIndex` GSI** — accepts TYPE_ID or human name, resolved with the build-generated `query-lambda/document-types.json` + the DynamoDB catalog; `GET /documents?field=&value=` searches any extracted field, indexed when combined with `docType`, full-table Scan otherwise).
- `src/document-processing/resources/enrichment-lambda/indexing.py` — Promotes the final classification to indexed DynamoDB columns (`DocumentType`, `ProcessedAt`, `DocumentTypeName`, `ValidationStatus`, `ContainedDocumentTypes`, `DocumentName`) so the query API filters by type without a Scan. Best-effort: never fails validation. Backfill older items with `pnpm backfill:index`.
- `documentacion/API_USO.md` — **User-facing API reference (all endpoints).** Keep it in sync — see "API Documentation" below.
- `catalogs/` — **THE single source of truth for document types.** One JSON per type (`<TYPE_ID>.json`, shape `{ "TYPE_ID": { ...entry } }`), plus `catalogs/_shared.json` (globals: `_field_sets`, `cross_document_validation_rules`, `_global_output_fields`) and `catalogs/_TEMPLATE.json` (copy this to create a new type). See `catalogs/README.md` for the full guide. `pnpm run build` combines this folder into the bundled `config/catalogs.json`; `pnpm sync` pushes it to DynamoDB for hot-reload.
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
| `pnpm build` | Combine `catalogs/*.json` → `config/catalogs.json`, patch the construct, `tsc`. Run before `deploy` / `diff` / `synth`. |
| `pnpm sync` | **Push the whole `catalogs/` folder to DynamoDB (hot-reload, no redeploy).** |
| `pnpm sync:one -- <selector>` | **Push ONE type: `--type TYPE_ID`, `--file path`, or a fuzzy name.** Add `--dry-run` to preview. |
| `pnpm synth` | Produce CloudFormation via `npx cdk synth`. |
| `pnpm run diff` | Show pending changes vs deployed stack. |
| `pnpm run deploy` | Deploy (`--require-approval never`). Defaults to **dev** (safe: never touches prod). |
| `pnpm run deploy:dev` / `deploy:prod` | Deploy a specific environment (`-c env=dev\|prod`). Prod is explicit. See below. |
| `pnpm run import-cert` | Import/renew the TLS cert into ACM for the custom domain. |
| `pnpm run destroy` / `pnpm run destroy:full` | Tear down stack (full variant cleans lingering resources). |
| `pnpm preview-prompts` | Render the actual prompts Bedrock will receive. Run after catalog edits (needs `pnpm build` first). |
| `pnpm backfill:index` | Rebuild the indexed type columns on documents processed before they existed (`-- --dry-run`, `-- --force`). |
| `pnpm setup:python` | Create the test venv. |
| `pnpm test-local` / `pnpm test-e2e` | Local handler tests and end-to-end pipeline tests. |
| `pnpm track` | Track a Step Functions execution. |

**Schema/catalog management is exactly two commands: `pnpm sync` (all) and `pnpm sync:one` (one).** There is no `add-type`, `db-sync`, `list-types`, etc. anymore — edit files under `catalogs/` and sync.

Python tests use pytest from the project venv (`.venv`): `pnpm test:unit` is offline; `pnpm test:pedimentos` hits real AWS (Textract + Bedrock).

## Environments & API security

Two environments via CDK context (`bin/app.ts` reads `-c env=dev|prod`, **default dev**, loads `.env.dev` / `.env.production`):
- **dev** (default) → stack `DocumentValidationStack-Dev`, **open** API on the random execute-api URL. For testing. A bare `pnpm run deploy` targets dev.
- **prod** → stack `DocumentValidationStack`, hardened: **API keys** (`x-api-key`) required on every method, **WAFv2** (rate-based + AWS managed rules), and a **custom domain** (`idp.alsuper.com`) when `DOMAIN_NAME`+`CERT_ARN` are set. All prod hardening is gated on `environment === "prod"` in `src/document-validation-stack.ts`.

Missing/invalid key → native 403 from API Gateway **without invoking the Lambda** (no backend cost for 401/403/404). Cert is bring-your-own: import with `scripts/import-cert.mjs` (renew in place with `--renew --arn` — zero downtime, no redeploy). External DNS: add a CNAME to the `CustomDomainTarget` output. Full guide: **`documentacion/AMBIENTES_Y_SEGURIDAD.md`**.

## Catalog Loading Precedence

When the enrichment Lambda runs, it loads types in this order (last wins):

1. `config/catalogs.json` — the single bundled catalog (built from `catalogs/*.json`), shipped with the Lambda code.
2. DynamoDB (`CATALOG_TABLE` env var) — overrides bundled entries when `enableDynamoCatalog: true`. This is what `pnpm sync` writes to.

`forceReloadCatalog: true` reloads on every invocation (small perf cost, immediate DynamoDB effect).

A type is routed to the multi-document customs pipeline (segmenter + cross-document rules) iff its entry declares `"pipeline": "expediente"`; otherwise it uses the standard single-document flow (`is_customs_type` reads this field).

## How To Extend

Pick the lowest-impact path that solves the request:

1. **New document type** → copy `catalogs/_TEMPLATE.json` to `catalogs/<TYPE_ID>.json`, edit it, then either `pnpm sync:one -- --type <TYPE_ID>` (immediate, DynamoDB) or `pnpm run build && pnpm run deploy` (bundled baseline). Full guide: `catalogs/README.md`. Re-syncing a type overwrites its DynamoDB row entirely.
2. **New extraction field or hint** → add it to `extraction_fields` / `extraction_hints` in that type's file under `catalogs/`. Hints are free-form Spanish describing *where* on the document the field lives.
3. **New rule type beyond regex / date_expiry / presence / boolean_true** → extend `validators/base.py` and the corresponding module under `validators/`.
4. **External API calls, cross-field checks, check digits** → create a `custom_validators/<name>.mjs` exporting `async function validate(entityMap, context)` that returns `[{ field, rule, valid, message }]`. Reference it via `custom_validator` in the type's catalog entry.

Avoid editing `handler.py` for feature work. It is the engine; features live in the catalog (`catalogs/`) and validators.

## API Documentation (keep in sync automatically)

`documentacion/API_USO.md` is the single source of truth for how to call the app's REST API. **Whenever you change the API surface, update that doc in the same change — do not defer it.** This applies when you:

- add, remove, or rename a route in `src/document-validation-stack.ts` (the `DocumentUploadApi` RestApi — `addResource` / `addMethod`);
- change a handler's request or response shape in `src/document-processing/resources/upload-lambda/` or `query-lambda/` (body fields, query params, status codes, response JSON);
- change auth, payload limits, or the base URL / stage.

For each such change: edit the matching section of `API_USO.md` (endpoint table + the per-endpoint request/response/examples), keep the curl and `pnpm` examples runnable, and match the existing Spanish tone. The doc carries a maintenance header comment repeating this rule.

**API Gateway gotcha:** adding a route via CDK does not always redeploy the `prod` stage — a new route can return `403 Missing Authentication Token` even after a successful `pnpm run deploy`. If so, force a stage redeploy: `aws apigateway create-deployment --rest-api-id <id> --stage-name prod --region us-east-1`.

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
