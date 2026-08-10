# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project Overview

**Documents Validator** is a document validation and extraction system on AWS: Amazon Bedrock (Claude) classifies and extracts fields, a Python Lambda applies deterministic validation rules defined in a JSON catalog (hot-reloadable via DynamoDB, no redeploy needed).

The engine is generic (any document type can be added via `catalogs/`), but **as of 2026-07-24 the project pivoted to Mexican customs (aduanal) documents**. The only catalog type currently active — read by `pnpm build`/`pnpm sync` — is `PEDIMENTO_ADUANAL` (`catalogs/PEDIMENTO_ADUANAL.json`), processed by a dedicated multi-document "expediente" pipeline (pedimentos, CFDIs, permits, VUCEM acknowledgements, etc., all inside one PDF). The older generic ID-document types (INE, Pasaporte, CSF, CURP, NOM-35, internal formats...) still exist as JSON but were moved to **`catalogs/save/`**, a folder the build/sync tooling does not read — they are parked, not deleted, and not part of the live system. Do not describe them as "supported out of the box" unless they're moved back into `catalogs/`.

## Documentation Map

This repo documents itself extensively in Spanish — **read the relevant doc instead of re-deriving things from code**, and update it in the same change if you touch what it describes.

| Doc | Covers |
|---|---|
| `README.md` | Human-facing entry point: what the project is, quickstart, links to everything below. |
| `catalogs/README.md` | **The catalog DSL spec** (field-centric v4) — field shapes, every `rule_type`, computed fields, `pipeline: "expediente"` semantics, gotchas. Read before touching any `catalogs/*.json`. |
| `documentacion/COMO_FUNCIONA.md` | High-level architecture walkthrough with diagrams — best first read to understand the system without reading code. |
| `documentacion/ARQUITECTURA.md` | **AWS architecture diagram + execution-flow diagram** (both pipeline branches) in one place, plus which Bedrock model is used and, per path, which extraction mechanism runs (AI-only vision / Textract-only / Textract+AI hybrid / pypdf "Python OCR" / pure code). Update its diagrams in the same change if you touch stack infra (`document-validation-stack.ts`) or `handler.py` routing. |
| `documentacion/FLUJO_EJECUCION.md` | Code-level execution trace, file:line references, both pipeline branches, dataflow table, and a ranked list of known dataflow inefficiencies. Read before touching `handler.py` or `pipeline/`. |
| `documentacion/GUIA_DESPLIEGUE_Y_USO.md` | Full operational guide: install, deploy, update/delete catalogs, troubleshooting table, file map of what to edit vs. never touch. |
| `documentacion/API_USO.md` | **REST API reference — keep in sync automatically, see below.** |
| `documentacion/APIS_INTEGRACION.md` | Integration-facing inventory: internal APIs this repo exposes vs. external (AWS) APIs it consumes, with exact SDK operations and file references. Update alongside `API_USO.md` when the API surface or AWS service calls change. |
| `documentacion/AMBIENTES_Y_SEGURIDAD.md` | dev vs prod stacks, custom domain, API keys, WAF. |
| `documentacion/TESTS.md` | Every test command, what it checks, cost, and how to read `pnpm comparar` output to diagnose a bug. |
| `documentacion/COSTOS.md` | Measured (not estimated) Bedrock/Textract/infra costs, per page/doc/month. |
| `documentacion/ENVIAR_DOCUMENTO_API.md` | How to send a document and read back a result via the CLI scripts or curl. |
| `catalogs/tipos-docs.md` | Informal notes mapping business document names to `TYPE_ID`s (customs domain). |
| `custom_validators/README.md` (inside enrichment-lambda) | How to write a Node.js custom validator. |

## Architecture

```
Upload API → S3 (raw/) → Step Functions (blueprint)
           → Bedrock Classification → Bedrock Extraction   [standard flow only]
           → Enrichment Lambda (handler.py) — branches on document type:
               ├── expediente pipeline (pipeline/) — customs, multi-document PDFs
               └── standard flow — single document, reuses Bedrock's extraction
           → EventBridge (APROBADO / RECHAZADO / REQUIERE_REVISION)
```

The Step Functions state machine itself comes from `@cdklabs/cdk-appmod-catalog-blueprints`; this repo only supplies the enrichment Lambda and the upload/query Lambdas. See `documentacion/FLUJO_EJECUCION.md` for the full trace with file:line references.

`raw/{TYPE?}/{uuid}/file` is only where the object lives *during* processing — the blueprint (`QueuedS3Adapter`) moves it to `processed/{documentId}/{filename}` on any execution that completes without an exception (including a RECHAZADO validation outcome — that's a business result, not an infra failure) or to `failed/{documentId}/{filename}` only when the execution itself throws/times out. `documentId` is exactly the uuid the Upload API returned (see `patches/default-sqs-consumer-index.py` — no timestamp/filename appended), so `GET /documents/{id}` resolves it with a direct DynamoDB `GetItem` (`query-lambda/index.mjs::getById` tries that first, falling back to a `begins_with` Scan only when it misses). Deterministic per S3 key also means the Step Functions execution name is deterministic: a genuinely duplicate S3/SQS delivery of the same object either no-ops (Standard Step Functions' own `StartExecution` idempotency, while the original execution is still running) or raises `ExecutionAlreadyExists`, caught in the same patch and skipped rather than retried into the DLQ. Code/docs that need to read the original bytes after the fact must account for the raw→processed/failed move; see `documentacion/APIS_INTEGRACION.md` §1.1/§1.5/§2.3.

Key pieces:

- `bin/app.ts` — CDK entry point. Reads `-c env=dev|prod` (default dev), loads `.env.dev`/`.env.production`, instantiates the stack.
- `src/document-validation-stack.ts` — Main CDK stack. Wires Upload/Query Lambdas, the Bedrock pipeline, Enrichment Lambda, DynamoDB catalog, EventBridge broker, and prod-only hardening (API keys, WAFv2, custom domain).
- `src/prompt-generator.ts` / `src/document-catalog.ts` — Generate Bedrock classification/extraction prompts at synth time from the **legacy DSL only** (`extraction_fields`/`extraction_hints`). They do not understand the field-centric `fields` DSL — `pnpm preview-prompts` shows no extraction fields for a field-centric type even though extraction works fine (it happens Python-side, not through this prompt).
- `src/document-processing/resources/enrichment-lambda/` — Python 3.12 validation engine (do not modify `handler.py` lightly; extend via catalog config and validators instead).
  - `config/catalogs.json` — **Bundled catalog. GENERATED — do not edit, gitignored.** Produced by `pnpm build` (`scripts/sync-catalogs.mjs`) from `catalogs/*.json`.
  - `document_catalog.py` — Loads catalog entries (bundled JSON + DynamoDB overrides); detects which DSL a type uses by the presence of the `"fields"` key.
  - `validators/` — `engine.py` is the dynamic rule engine and the sole arbiter of which `rule_type`s exist (`_dispatch_rule`). `base.py`/`identification.py`/`fiscal.py`/`internal_formats.py` back the legacy/standard flow's `validator_module` mechanism. `validators/customs/*` are thin shims — customs rule *logic* lives in the catalog JSON and in `pipeline/expediente.py`.
  - `pipeline/` — the expediente pipeline: `pdf_pages.py` (pypdf layout text + parallel Textract OCR fallback per scanned page; `load_image_page` is the equivalent single-page entry point for a standalone JPG/JPEG/PNG — e.g. a photographed ID — Textract reads those bytes directly, no pypdf/PDF-wrapping involved, since a field-centric type like `INE` isn't necessarily backed by a PDF; `process_pdf`'s `source_format` param picks the right one, defaulting to the PDF path unchanged), `segmenter.py` (Bedrock page-range classification with deterministic markers, or a single segment if the caller declared the type), `text_extractor.py` (catalog-schema extraction incl. nested/repeatable fields, chunked, parallel per-chunk), `expediente.py` (orchestrator: per-segment extraction — parallelized via `ThreadPoolExecutor`, `EXTRACT_WORKERS` env var — cross-document checks, tracking/provenance), `liquidacion.py` (fills `LIQUIDACION_NORMALIZADA` computed fields via concept-alias/source/filter mapping declared directly on catalog fields), `normalize.py` (deterministic amounts/dates/pedimento-number/UUID normalization + regex reference recovery), `qr_recovery.py` (corrects PATENTE/NUMERO_CONSECUTIVO/ANIO_PEDIMENTO/ADUANA_DESPACHO from the pedimento's embedded QR, gated by `QR_RECOVERY_ENABLED`), `xml_document.py` (deterministic `ElementTree`-based sibling to `expediente.py` for types whose fields declare `xpath` instead of `hint` — no Bedrock, no OCR; `handler.py` routes to it by the uploaded file's `.xml` extension, not by TYPE_ID; see `catalogs/README.md` → "Tipos con fuente XML"), `tabular_document.py` (same idea for `.xlsx`/`.xlsm`/`.csv`: fields declare `cell`/`column` instead of `hint`, extraction is a grid lookup via `openpyxl`/stdlib `csv` — see `catalogs/README.md` → "Tipos con fuente tabular"), `result_common.py` (tracking/provenance and error-result shape shared by `expediente.py`/`xml_document.py`/`tabular_document.py` so all three pipelines' results are interchangeable to `handler.py`).
  - `extractors/` — Textract wrappers (`textract_analyze_id.py`, `textract_bedrock.py`) used by the standard flow's optional re-extraction fallback.
  - `indexing.py` — Promotes the final classification to indexed DynamoDB columns (`DocumentType`, `ProcessedAt`, `DocumentTypeName`, `ValidationStatus`, `ContainedDocumentTypes`, `DocumentName`, `Tracking`) so the query API filters by type without a Scan. `Tracking` (a Map) mirrors the envelope's top-level `tracking` object field-for-field in PascalCase (`_TRACKING_FIELD_MAP`: CreatedAt/UpdatedAt/ProcessingStartedAt/ProcessingCompletedAt/ProcessingSeconds/ProcessingTime/Schema/AppOrigin/ModelIds) — full parity with the nested `expediente.tracking`, never a cut-down subset — on the item itself rather than only inside the `EnrichmentResult` JSON blob. Floats (`ProcessingSeconds`/`ProcessingTime`, and any future float nested in `Schema`) go through `_decimalize()` recursively — boto3's resource API rejects a raw float anywhere in the structure. Best-effort: never fails validation. Backfill older items with `pnpm backfill:index`.
  - `custom_validators/*.mjs` — Optional Node.js custom validators for logic beyond regex (standard flow only, see below).
- `src/document-processing/resources/upload-lambda/index.mjs` — Node.js 20 REST upload endpoint (`POST /upload`, inline or S3-presigned). Resolves the caller's `x-api-key` against the `AppsTable` DynamoDB table to a human-readable app name, carried as `app-origin` S3 object metadata (same channel as `model-id`) for traceability.
- `src/document-processing/resources/query-lambda/index.mjs` — Node.js 20 REST read/search endpoints (`GET /documents/{id}`, `GET /documents/{id}/file`, `GET /documents?docType=...&field=...&value=...`). Enforces per-app document access (`resolveCallerApp`/`canSeeDocument`, reading `AccessScope`/`AllowedDocTypes`/`AllowFieldSearch` from the Apps table — see `documentacion/AMBIENTES_Y_SEGURIDAD.md` → "Acceso por app") — a denied single document is `404`, a denied `docType`/`field` search is `403`.
- `catalogs/` — **THE single source of truth for document types**, but only for files that live directly in this folder (no subfolders are read). See "Catalog DSL" below and `catalogs/README.md` for the full spec.
- `scripts/` — CLI tooling: catalog sync, prompt preview, local/e2e processing, comparisons, backfill, app/API-key registration (`manage-apps.mjs`). See the Commands table.
- `src/document-processing/multi-model-document-processing.ts` — Subclass of the blueprint's `BedrockDocumentProcessing` that points the classification/extraction steps at our own Lambda (`resources/model-invoke-lambda/`, Bedrock **Converse** API) so any Bedrock model works, not just Anthropic. Uses the construct's documented `classificationStep()`/`processingStep()` extension points — **do not** move this back to patching `node_modules`. Its overrides run during `super()`, so their state lives in a module-level `WeakMap`, never in class fields (with `target: es2022`, field initializers run after `super()` and would wipe it).
- `patches/default-sqs-consumer-index.py` — Patch applied by `scripts/patch-construct.mjs` during `pnpm build`. Also reads the uploaded object's `app-origin` S3 metadata (best-effort `head_object`) and forwards it as `appOrigin` on the Step Functions input, so it reaches the enrichment Lambda's result envelope for every document, not just ones that trigger re-extraction.
- **`AppsTable`** (`document-validation-apps[-dev]`, DynamoDB, one per stack) — registry of per-app API keys/usage plans created via `pnpm apps:register` (not CDK-managed beyond the table itself; see `scripts/manage-apps.mjs` and `documentacion/AMBIENTES_Y_SEGURIDAD.md`). PK `ApiKeyId`. Read by the Upload Lambda, written only by the CLI script.
- **`CostTable`** (`document-validation-costs[-dev]`, DynamoDB, `RETAIN`) — per-document AWS cost audit trail, separate from the tracking table on purpose (finance/audit concern, not a document-query one). PK `DocumentId`. Written best-effort by `enrichment-lambda/cost_audit.py` (`handler()`, after `write_index_attributes`) with a $ breakdown per service — Bedrock segmentation/extraction/upstream classification+processing, Textract OCR, an approximate Lambda compute cost (`pipeline/pricing.py` has the rates). Read by `pnpm costos` (`scripts/ver-costos.py`). Full detail: `documentacion/COSTOS.md` §7.

## Catalog DSL (field-centric v4)

`PEDIMENTO_ADUANAL.json` uses the current DSL: each field is declared **once**, as a key of a `fields` dict, carrying its type, whether it's required, its extraction hint, and its own `validation` — instead of being repeated across `extraction_fields`/`extraction_hints`/`required_fields`/`validation_rules` (the legacy DSL, still supported by the engine for types that use it, but no active type does today).

```jsonc
"NOMBRE_DEL_CAMPO": {
  "type": "string",              // string | number | boolean | date | object | array
  "required": true,
  "hint": "Dónde vive el dato en el documento, en español.",
  "validation": [ { "rule_type": "regex", "pattern": "...", "message": "..." } ],
  "fields": { /* only if type == "object": same shape, recursive */ },
  "item_fields": { "$common": [...], /* only if type == "array" of rows */ },
  "computed": true                 // only if the pipeline fills it, not the model
}
```

- The DSL a type uses is detected purely by the presence of the `"fields"` key in its raw JSON (`document_catalog.py`) — never hardcode logic to a specific `TYPE_ID`; detect by DSL shape so any future type gets the same support for free.
- A type with `"fields"` is routed to the expediente pipeline automatically (extraction requires it — the legacy prompt generator can't read `fields`), even without `pipeline: "expediente"`. That flag still controls two separate things: auto-segmentation candidacy for undeclared uploads, and eligibility for cross-document rules. See `catalogs/README.md` → "`pipeline: 'expediente'`: qué controla realmente" for the exact rules.
- `validators/engine.py` is the only place that knows real `rule_type`s. An unrecognized `rule_type` doesn't error — it's silently recorded as `skipped: true, severity: "warning"`. Always check for `"skipped": true` in returned checks before assuming a new rule is miscalibrated.
- Full spec (every `rule_type`, the `LIQUIDACION_NORMALIZADA` computed-field convention, `$common` item fields, cross-document rules, gotchas): **`catalogs/README.md`**.

## Output shape

Each resolved field — scalar, object, or array, at any nesting depth — resolves recursively:
- scalar → `{value, confidence, valid, reason, source}`
- object → `{valid, reason, fields: {SUBCAMPO: {...recursive...}}}`
- array → `{items: [ {...recursive per row...} ]}`

`source` is `"MODEL_EXTRACTION"` or `"NORMALIZED"` (computed/recovered post-extraction) — distinct from a field's `hint`, which is catalog input describing where the value lives on the document for the prompt. `extractedValue` is always the verbatim model output (or `null` — never invented); `normalizedValue`/computed values are a deterministic derivation that never overwrites it. Full request/response shapes, including the DynamoDB-compact-vs-S3-complete split: `documentacion/API_USO.md`.

## Environment

- Node.js 20+, pnpm 10+ (package manager pinned: `pnpm@10.20.0`).
- AWS CDK v2 (`aws-cdk-lib ^2.252.0`), deployed via `npx cdk`.
- Python 3.12 for the enrichment Lambda; local test venv via `pnpm setup:python`.
- `.env.dev` / `.env.production` (gitignored, template `.env.example`) hold `BEDROCK_MODEL_ID`, `CDK_DEFAULT_ACCOUNT`, `CDK_DEFAULT_REGION`, `CATALOG_TABLE`, `AWS_REGION`, prod-only `DOMAIN_NAME`/`CERT_ARN`, and `QR_RECOVERY_ENABLED`.

## Commands

Run everything with pnpm from the repo root.

| Command | Purpose |
| --- | --- |
| `pnpm build` | Combine `catalogs/*.json` → `config/catalogs.json`, patch the construct, `tsc`. Run before `deploy`/`diff`/`synth`. |
| `pnpm sync` | **Push the whole `catalogs/` folder to DynamoDB (hot-reload, no redeploy).** |
| `pnpm sync:one -- <selector>` | **Push ONE type: `--type TYPE_ID`, `--file path`, or a fuzzy name.** Add `--dry-run` to preview. |
| `pnpm synth` | Produce CloudFormation via `npx cdk synth`. |
| `pnpm run diff` / `diff:dev` / `diff:prod` | Show pending changes vs. the deployed stack (default/dev/prod). |
| `pnpm run deploy` | `pnpm build` + deploy. Defaults to **dev** (safe: never touches prod). |
| `pnpm run deploy:dev` / `deploy:prod` | Deploy a specific environment. Prod is explicit. See `documentacion/AMBIENTES_Y_SEGURIDAD.md`. |
| `pnpm run import-cert` | Import/renew the TLS cert into ACM for the prod custom domain. |
| `pnpm apps:register` / `apps:rotate` / `apps:reconfigure` / `apps:grant` / `apps:deny` / `apps:disable` / `apps:enable` / `apps:revoke` / `apps:adopt` / `apps:list` | Register/rotate/reconfigure/grant/deny/disable/enable/revoke a per-app API key + usage plan (`scripts/manage-apps.mjs`), or adopt an existing key (e.g. `idp-default`) into the traceability table — no redeploy needed. `reconfigure` changes only rate-limit/burst-limit/quota-per-day without touching the key; `disable`/`enable` toggle a key without deleting anything (`revoke` = `disable` + optional `--delete`). `grant`/`deny --doctype A,B` add/remove specific `AllowedDocTypes` (union/difference against what the app already has) without retyping the full list, unlike `reconfigure --allowed-doctypes` which replaces it outright. Every mutating subcommand is interactive (prompts for missing args) and always confirms `(S/n)` before touching AWS, unless `--yes`/no TTY. `-- --name <appName> --env dev\|prod [...]`. Full lifecycle, per-app document access control, backend mechanics, the enable/disable propagation-delay gotcha, and required IAM permissions: `documentacion/AMBIENTES_Y_SEGURIDAD.md`. |
| `pnpm run destroy` / `destroy:full` | Tear down stack (full variant also removes the DynamoDB catalog table — irreversible for DynamoDB-only types). |
| `pnpm preview-prompts` | Render the Bedrock prompts the **standard flow** will receive (legacy DSL only; doesn't reflect field-centric types). Run after catalog edits, needs `pnpm build` first. |
| `pnpm backfill:index` | Rebuild indexed type columns on documents processed before they existed (`-- --dry-run`, `-- --force`). |
| `pnpm setup:python` | Create the `.venv` test environment. |
| `pnpm test:unit` | Offline pytest: rule engine, normalizers, catalog/ground-truth sanity. Free. |
| `pnpm test:pedimentos` | Real pipeline (`process_pdf()` in-process — no S3/SQS/Step Functions) against `test-docs/pedimentos docs/docs`, asserts against `tests/integration/expected_pedimentos.json`. AWS creds needed, ~$1/full run. `-- --file <name>` for one doc, `-- --save <dir>` to keep full JSON results. |
| `pnpm comparar` (`scripts/comparar-documento.sh`) | Same comparison as `test:pedimentos` but for a single document with a confirmation prompt — the go-to for "is this a bug" triage. |
| `pnpm procesar` (`scripts/procesar-docs.py`) | Run the real pipeline locally against your own docs (no stack needed, your AWS creds), no assertions. Every run gets its own numbered folder that's never overwritten — `resultados/[<model>/]NNN/` (001, 002, ...) — holding `<file>.json` + `_resumen.md` (real cost/page, timing, and per-field validity benchmark, updated after each doc) + `_log-<date>.txt` (full console capture), so past runs stay as history for comparison. Up to 3 positionals, in order: `PATH [TYPE] [MODEL]` — PATH is a file OR a folder, auto-detected; TYPE/MODEL are shorthand for `--type`/`--model` (a `--flag` wins if both are given, with a mismatch warning). `--file`/`--dir`/`--type`/`--model` still work as explicit flags. `-- <path> [type] [model] --importer RFC --out`. |
| `pnpm resultado` (`scripts/ver-resultado.py`) | Fetch and pretty-print a document's result from a deployed stack by id/prefix. `-- --id <id> [--wait] [--save dir] [--env prod]`. |
| `pnpm costos` (`scripts/ver-costos.py`) | **Real per-document AWS cost audit** — reads `document-validation-costs[-dev]` (written by `cost_audit.py` on every enrichment Lambda invocation). No args: aggregate totals by document type + recent documents. `-- --id <id>`: full per-service $ breakdown for one document. `-- --env prod`. See `documentacion/COSTOS.md` §7. |
| `pnpm enviar` (`scripts/enviar-documento.sh`) | Upload a document to a deployed stack's API (auto-picks inline vs. S3-presigned by size). `pnpm enviar file.pdf TYPE [--env dev\|prod]`. |
| `pnpm test-local` / `test-e2e` | Local handler tests with mocked entities; end-to-end test against a deployed stack's real API (no ground truth comparison). |
| `pnpm test:e2e:pedimento` (`scripts/test-e2e-pedimento.py`) | **Real infra E2E**: uploads via `POST /upload` to a deployed stack, waits for the actual Step Functions execution, checks it `SUCCEEDED` (catches infra bugs `pnpm comparar` can't see), *and* compares against ground truth. |
| `pnpm schema:sensitivity` (`scripts/schema-sensitivity.py`) | Mutates the catalog and re-measures ground-truth/rule outcomes to answer "what does changing this do" with data. `validation`-category mutations are free (re-evaluated on one baseline extraction); `extraction`-category mutations each cost a real Bedrock call (~$0.05). `-- --list`/`--file`/`--only`/`--skip-paid`. |
| `pnpm track` (`scripts/track-execution.mjs`) | Follow a Step Functions execution (`--latest --follow`, or `--execution-arn`). |

Full test matrix with costs and troubleshooting: **`documentacion/TESTS.md`**.

## Environments & API security

Two environments via CDK context (`bin/app.ts` reads `-c env=dev|prod`, **default dev**, loads `.env.dev`/`.env.production`):

- **dev** (default) → stack `DocumentValidationStack-Dev`, **open** API on the random execute-api URL. For testing. A bare `pnpm run deploy` targets dev.
- **prod** → stack `DocumentValidationStack`, hardened: **API keys** (`x-api-key`) required on every method, **WAFv2** (rate-based + AWS managed rules), and a **custom domain** (`idp.alsuper.com`) when `DOMAIN_NAME`+`CERT_ARN` are set. All prod hardening is gated on `environment === "prod"` in `src/document-validation-stack.ts` (`isProd`, `src/document-validation-stack.ts:96`).

Each environment is a fully isolated stack (own Lambdas, Step Functions, S3, DynamoDB tracking table, KMS, EventBus) with a `-dev` resource suffix for globally-unique names; both share the imported `document-validation-catalog` DynamoDB table. Missing/invalid key → native 403 from API Gateway **without invoking the Lambda** (no backend cost for 401/403/404). Cert is bring-your-own: import with `scripts/import-cert.mjs` (renew in place with `--renew --arn` — zero downtime, no redeploy). Full guide: **`documentacion/AMBIENTES_Y_SEGURIDAD.md`**.

## Catalog Loading Precedence

When the enrichment Lambda runs, it loads types in this order (last wins):

1. `config/catalogs.json` — the single bundled catalog (built from `catalogs/*.json`), shipped with the Lambda code.
2. DynamoDB (`CATALOG_TABLE` env var) — overrides bundled entries when `enableDynamoCatalog: true` (default in `bin/app.ts`). This is what `pnpm sync`/`sync:one` write to.

`forceReloadCatalog: true` (default) reloads on every invocation (small perf cost, immediate DynamoDB effect).

Routing to the expediente pipeline (`pipeline/expediente.py`) happens if **any** of: `pipeline == "expediente"`, the event declares `expediente: true` explicitly, or the type declares `fields` (field-centric DSL — see above). Auto-segmentation candidacy for undeclared uploads and eligibility for cross-document rules still depend exclusively on `pipeline: "expediente"`.

## How To Extend

Pick the lowest-impact path that solves the request:

1. **New document type** → pick the template that matches the SOURCE, not the business document — the same invoice can need a different template depending on what actually arrives (a scanned/printed PDF vs. the native CFDI XML vs. an Excel export are three different sources for the same real-world document, and often three different catalog types):
   - **PDF/scan (a model has to read it)** → `cp catalogs/_TEMPLATE.json catalogs/<TYPE_ID>.json`, edit using the field-centric DSL with `hint` per field (see `catalogs/README.md`).
   - **Already-structured XML** (CFDI, VUCEM, any fixed-schema XML) → `cp catalogs/_TEMPLATE_XML.json catalogs/<TYPE_ID>_XML.json`, edit with `xpath`/`namespaces`/`item_xpath` per field instead of `hint` (see `catalogs/README.md` → "Tipos con fuente XML", and `catalogs/FACTURA_HONORARIOS_XML.json` for a real, verified example). Deterministic extraction via `pipeline/xml_document.py` — **zero Bedrock cost**, but `documentType` is mandatory at upload (no vision-based auto-classification for raw XML).
   - **Already-structured tabular data** (XLSX report, CSV export) → `cp catalogs/_TEMPLATE_TABULAR.json catalogs/<TYPE_ID>_XLSX.json`, edit with `cell`/`column`/`column_letter`/`header_row` per field instead of `hint` (see `catalogs/README.md` → "Tipos con fuente tabular"). Deterministic extraction via `pipeline/tabular_document.py` — **zero Bedrock cost** for XLSX; for CSV specifically, `documentType` is not just recommended but required to stay zero-cost (see the gotcha in that doc section). Same file/type serves both `.xlsx` and `.csv` if the column layout matches.

   Any of the three: edit the copied template (delete the `__NOTAS__`/`__COMENTARIO_*__` keys — documentation only, the loader ignores them but they don't belong in a real type), then `pnpm sync:one -- --type <TYPE_ID>` (immediate, DynamoDB) or `pnpm run build && pnpm run deploy` (bundled baseline). Re-syncing a type overwrites its DynamoDB row entirely. A type parked in `catalogs/save/` can be revived by moving its file back into `catalogs/` — but review it against the current field-centric DSL first, since those files predate it. Any `catalogs/_*.json` file (not just the three templates above) is excluded from build/sync by filename prefix, not by an allowlist — see `scripts/sync-catalogs.mjs`/`sync-schema.mjs`.
2. **New/changed extraction field or hint** → edit `fields.<NOMBRE>.hint`/`type`/`required` in that type's file under `catalogs/`. Hints are free-form Spanish describing *where* on the document the field lives.
3. **New `rule_type`** → extend `validators/engine.py` (`_dispatch_rule` + a `_check_<name>` function). Applies to every type automatically, config-driven from then on.
4. **New cross-document rule** (compares sibling documents inside one expediente) → needs real code, not just JSON: add a `rule_type` to `_shared.json` → `cross_document_validation_rules` *and* its logic in `pipeline/expediente.py::_run_cross_document_rules`.
5. **External API calls, cross-field checks, check digits — standard flow only** → create a `custom_validators/<name>.mjs` exporting `async function validate(entityMap, context)`, reference it via `custom_validator` in the type's catalog entry. Does **not** run for field-centric/expediente types (`PEDIMENTO_ADUANAL` included) — for those, use `cross_field_validation` in the catalog, a cross-document rule, or extend `engine.py` directly.

Avoid editing `handler.py` for feature work. It is the engine; features live in the catalog (`catalogs/`) and validators.

## API Documentation (keep in sync automatically)

`documentacion/API_USO.md` is the single source of truth for how to call the app's REST API. **Whenever you change the API surface, update that doc in the same change — do not defer it.** This applies when you:

- add, remove, or rename a route in `src/document-validation-stack.ts` (the `DocumentUploadApi` RestApi — `addResource`/`addMethod`);
- change a handler's request or response shape in `src/document-processing/resources/upload-lambda/` or `query-lambda/` (body fields, query params, status codes, response JSON);
- change auth, payload limits, or the base URL/stage.

For each such change: edit the matching section of `API_USO.md` (endpoint table + the per-endpoint request/response/examples), keep the curl and `pnpm` examples runnable, and match the existing Spanish tone. The doc carries a maintenance header comment repeating this rule.

**API Gateway gotcha:** adding a route via CDK does not always redeploy the `prod` stage — a new route can return `403 Missing Authentication Token` even after a successful `pnpm run deploy`. If so, force a stage redeploy: `aws apigateway create-deployment --rest-api-id <id> --stage-name prod --region us-east-1`.

## Conventions

- TypeScript strict-ish CDK code, 2-space indent, double quotes, semicolons (see `src/document-validation-stack.ts`).
- Python is type-hinted and docstring-heavy (see `handler.py`). Keep it that way.
- Document IDs, field names, and classification labels in catalog entries are UPPER_SNAKE_CASE and Spanish (`NUMERO_PEDIMENTO`, `RFC_IMPORTADOR_EXPORTADOR`, `PEDIMENTO_ADUANAL`).
- User-facing messages and docs in the existing codebase are Spanish; match that when editing existing files. New files may be English if the user asks.
- Canonical validation status values: `APROBADO`, `RECHAZADO`, `REQUIERE_REVISION` (top-level); expediente/document-level uses `APPROVED`/`REJECTED`/`PARTIALLY_APPROVED`/`REVIEW_REQUIRED`/`PROCESSING_ERROR`; field-level uses `VALIDATED`/`INVALID`/`LOW_CONFIDENCE`/`NOT_FOUND`/`NOT_APPLICABLE`/`REVIEW_REQUIRED`. Do not invent new ones.
- Do not commit `.env.dev`, `.env.production`, or anything under `cdk.out/`, `dist/`, `node_modules/`, `config/catalogs.json`.

## Safety Notes For Agents

- This stack touches AWS (Bedrock, Textract, Lambda, API Gateway, DynamoDB, EventBridge, S3, WAFv2, ACM). Treat `pnpm run deploy*`, `pnpm run destroy*`, and any `aws dynamodb`/`aws apigateway` write command against a real account as high-risk and confirm with the user before running.
- The DynamoDB catalog table (`document-validation-catalog`) has `RETAIN` semantics (imported via `fromTableName`, shared across dev/prod). `pnpm run destroy` does not remove it; `pnpm run destroy:full` does — irreversible for types that only ever existed in DynamoDB.
- `iam-policy.json` defines the broad Bedrock invoke permissions the pipeline needs. Do not narrow it without verifying inference-profile ARNs still resolve.
- Test documents under `test-docs/` and events under `test-events/` are for local testing only; keep any PII out of commits.
- Most AWS-touching test/CLI commands cost real money (Bedrock + Textract) — see the cost column in `documentacion/TESTS.md` and the measured figures in `documentacion/COSTOS.md` before running a full suite repeatedly.

## Validation Before Handing Off

After changes, at minimum:

1. `pnpm build` to ensure TypeScript and the patch script succeed.
2. If the catalog changed: `pnpm sync:one -- --type <TYPE_ID> --dry-run` to validate marshalling, and — only if it's a legacy-DSL type — `pnpm preview-prompts` to confirm prompts still render (field-centric types won't show anything meaningful there).
3. If Python code changed, run `pnpm test:unit` (free) and, when the change could affect extraction/validation of pedimentos, `pnpm test:unit` first then flag to the user that `pnpm test:pedimentos`/`pnpm comparar` cost real money and should be run deliberately.
4. Mention any deploy, `sync`, or DynamoDB steps the user still needs to execute.

## Authors

This project is authored by:

Luis Hernández M.

## Contributors
