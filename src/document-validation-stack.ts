import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cwActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as events from "aws-cdk-lib/aws-events";
import * as eventTargets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sns from "aws-cdk-lib/aws-sns";
import * as subscriptions from "aws-cdk-lib/aws-sns-subscriptions";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { EventbridgeBroker } from "@cdklabs/cdk-appmod-catalog-blueprints";
import { Construct } from "constructs";
import { MultiModelDocumentProcessing } from "./document-processing/multi-model-document-processing";
import {
  generateClassificationPrompt,
  generateProcessingPrompt,
} from "./prompt-generator";

/**
 * GSI que indexa el tipo de documento (PK DocumentType, SK ProcessedAt).
 * Lo escribe `enrichment-lambda/indexing.py` y lo consulta el query Lambda.
 */
export const DOCUMENT_TYPE_INDEX = "DocumentTypeIndex";

export interface DocumentValidationStackProps extends cdk.StackProps {
  /**
   * Enable DynamoDB-backed catalog for runtime hot-reload of document types.
   * When true, creates a DynamoDB table and configures the Lambda to read from it.
   * Types in DynamoDB override types in the local JSON config.
   * @default false
   */
  enableDynamoCatalog?: boolean;

  /**
   * Force catalog reload on every Lambda invocation.
   * Useful with DynamoDB catalog for immediate changes.
   * Has a small performance cost.
   * @default false
   */
  forceReloadCatalog?: boolean;

  /**
   * Enable the DynamoDB-backed ordered fallback-model list: when a Bedrock
   * call fails because the requested model itself is unavailable (not
   * granted, doesn't exist, or errors/times out — see
   * `pipeline/bedrock.py::_MODEL_UNAVAILABLE`), the enrichment Lambda
   * retries the SAME request against the next model in this table instead
   * of failing outright. Same import-not-create, RETAIN pattern as
   * `enableDynamoCatalog` (see `documentacion/GUIA_DESPLIEGUE_Y_USO.md` for
   * the one-time `aws dynamodb create-table` step). Entirely optional even
   * when true — the Python side falls back to a hardcoded default order
   * (`pipeline/model_fallback.py::DEFAULT_FALLBACK_MODELS`) whenever the
   * table is unset, empty, or unreachable.
   * @default false
   */
  enableFallbackModels?: boolean;

  /**
   * Deployment environment. In "prod" the API is hardened: API keys required,
   * WAF (rate limiting + managed rules), and a custom domain when configured.
   * In "dev" the API stays open on the random execute-api URL for easy testing.
   * @default "dev"
   */
  environment?: "dev" | "prod";

  /**
   * Custom domain for the API (prod only), e.g. "idp.alsuper.com". Requires
   * `certArn`. When either is unset, the random execute-api URL is used.
   */
  domainName?: string;

  /**
   * ARN of an ACM certificate (same region) covering `domainName`. Import your
   * cert with `scripts/import-cert.mjs` and paste the ARN here (env CERT_ARN).
   */
  certArn?: string;

  /**
   * Per-IP request cap (WAF rate-based rule, requests per 5 min). Prod only.
   * @default 2000
   */
  wafRateLimit?: number;

  /**
   * Usage plan throttle (steady-state requests/second) and daily quota. Prod only.
   * @default { rateLimit: 20, burstLimit: 40, quotaPerDay: 5000 }
   */
  apiThrottle?: { rateLimit?: number; burstLimit?: number; quotaPerDay?: number };

  /**
   * In dev, require `x-api-key` on every method just like prod. Per-app keys
   * and usage plans are registered against the deployed stack with
   * `pnpm apps:register` (see scripts/manage-apps.mjs) — not managed by CDK,
   * unlike the single prod `idp-default` key below. Ignored in prod (always
   * requires a key there). Changing this needs a redeploy — there is no
   * runtime toggle.
   * @default false
   */
  devApiKeyRequired?: boolean;
}

export class BedrockDocumentProcessingStack extends cdk.Stack {
  constructor(
    scope: Construct,
    id: string,
    props?: DocumentValidationStackProps
  ) {
    super(scope, id, props);

    const enableDynamo = props?.enableDynamoCatalog ?? false;
    const forceReload = props?.forceReloadCatalog ?? false;
    const enableFallbackModels = props?.enableFallbackModels ?? false;
    // Base model for every step. Each step below can be pointed at a different
    // Bedrock model — any model, not just Anthropic — to trade cost against
    // accuracy per step. Unset overrides fall back to this one, so
    // BEDROCK_MODEL_ID alone still configures the whole pipeline.
    //
    // Not every Bedrock model supports everything a step needs. Before moving
    // a step, check the model offers: Converse; forced tool use (the enrichment
    // extractor relies on it for 100+ field catalogs); prompt caching (ignored
    // silently when unsupported — no error, but the cache discount is lost);
    // and vision, which only the classification/extraction steps here need.
    const bedrockModel =
      process.env.BEDROCK_MODEL_ID || "us.anthropic.claude-haiku-4-5-20251001-v1:0";
    // Vision-capable steps (they receive the document image/PDF itself).
    const classificationModel =
      process.env.BEDROCK_CLASSIFICATION_MODEL_ID || bedrockModel;
    const processingModel =
      process.env.BEDROCK_PROCESSING_MODEL_ID || bedrockModel;

    // Environment gate: prod hardens the API (keys + WAF + custom domain);
    // dev stays open on the random execute-api URL.
    const environment = props?.environment ?? "dev";
    const isProd = environment === "prod";
    // Suffix for account-global resource names (EventBus and the prod-only KMS
    // alias) so dev can coexist with prod. Prod keeps the bare names it was
    // first deployed with.
    const envSuffix = isProd ? "" : "-dev";

    // Prod uses ONE customer-managed key for the complete pipeline, including
    // S3. Dev deliberately creates no customer-managed KMS resources: every
    // service keeps its AWS-owned/default encryption instead. The blueprint is
    // patched by scripts/patch-construct.mjs to support that mode and to reuse
    // this key for the adapter bucket instead of minting a second CMK.
    const encryptionKey = isProd
      ? new kms.Key(this, "PipelineEncryptionKey", {
          enableKeyRotation: true,
          alias: "document-validation/pipeline",
          // CostTable is RETAIN, so its encryption key must outlive a normal
          // stack destroy too. Retiring it requires an explicit audited cleanup.
          removalPolicy: cdk.RemovalPolicy.RETAIN,
        })
      : undefined;

    // EventBridge broker for pipeline notifications
    const broker = new EventbridgeBroker(this, "IDPBroker", {
      name: `idp-broker${envSuffix}`,
      eventSource: "document-validation-pipeline",
      kmsKey: encryptionKey,
      useCustomerManagedEncryption: isProd,
    });

    // Generate prompts dynamically from the JSON config catalog
    const classificationPrompt = generateClassificationPrompt();
    const processingPrompt = generateProcessingPrompt();

    // Optional: DynamoDB catalog table for runtime hot-reload
    // Uses existing table if it already exists (RETAIN policy = never deleted on stack destroy)
    let catalogTable: dynamodb.ITable | undefined;
    if (enableDynamo) {
      // Cada entorno usa su propia tabla: prod conserva el nombre original y
      // dev apunta a `document-validation-catalog-dev`, para que editar el
      // catálogo en dev (pnpm sync) nunca toque el catálogo de producción.
      // `CATALOG_TABLE` de .env.dev / .env.production (cargado en bin/app.ts)
      // manda; el sufijo es solo el default si no está definido.
      const tableName =
        process.env.CATALOG_TABLE || `document-validation-catalog${envSuffix}`;

      // Import existing table to avoid "already exists" errors on redeploy
      // The table persists independently of the stack lifecycle
      catalogTable = dynamodb.Table.fromTableName(
        this,
        "DocumentCatalogTable",
        tableName
      );
    }

    // Optional: DynamoDB ordered fallback-model list (see
    // `enableFallbackModels` above for what this controls). Same
    // import-not-create pattern as the catalog table: the table survives
    // `pnpm run destroy`, and its absence is never fatal — the Python side
    // just uses its hardcoded default order instead.
    let fallbackModelsTable: dynamodb.ITable | undefined;
    if (enableFallbackModels) {
      fallbackModelsTable = dynamodb.Table.fromTableName(
        this,
        "FallbackModelsTable",
        "document-validation-fallback-models"
      );
    }

    // Enrichment Lambda - Config-driven Python validation engine
    //
    // Bundling: requirements.txt (pypdf) must ship with the code. We try a
    // local `pip install` first (fast, no Docker); if pip is unavailable the
    // CDK falls back to the official Python bundling container.
    const enrichmentDir = path.join(
      __dirname,
      "document-processing/resources/enrichment-lambda"
    );
    const enrichmentFn = new lambda.Function(this, "EnrichmentLambda", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(enrichmentDir, {
        exclude: ["__pycache__", "**/__pycache__", "*.pyc"],
        // CDK's default AssetHashType.SOURCE hashes only enrichmentDir's own
        // files, not the bundling command/environment that turns
        // requirements.txt into installed packages. That silently bit us: a
        // fix to the bundling command below (forcing manylinux cp312 wheels)
        // changed no file inside enrichmentDir, so the asset hash stayed
        // identical to the previous (broken cp313-numpy) build, and `cdk
        // deploy` kept reusing that already-published S3 object no matter
        // how many times it ran or how thoroughly cdk.out was cleared.
        // OUTPUT hashes the bundled result instead, so any change to how
        // dependencies get resolved always produces a new asset.
        assetHashType: cdk.AssetHashType.OUTPUT,
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          command: [
            "bash",
            "-c",
            "pip install -r requirements.txt -t /asset-output --no-cache-dir --quiet && cp -au . /asset-output",
          ],
          local: {
            tryBundle(outputDir: string): boolean {
              const { execSync } = require("child_process");
              try {
                // --platform/--python-version/--implementation/--abi force pip
                // to fetch manylinux cp312 wheels regardless of which Python
                // this machine's own `pip3` happens to be (a real bug we hit:
                // opencv-python-headless pulls in numpy, and a dev machine on
                // Python 3.13 silently resolved a cp313 numpy wheel that
                // loaded fine locally but failed at runtime on the actual
                // Python 3.12 Lambda — "Importing the numpy C-extensions
                // failed ... incompatible with python 'cpython-312'").
                // --only-binary=:all: is required alongside --platform (pip
                // refuses to build an sdist for a platform that isn't the
                // host). pypdf/pypdfium2/opencv-python-headless all ship
                // manylinux2014 cp312 wheels, so this never falls back to
                // building from source.
                execSync(
                  `pip3 install -r "${enrichmentDir}/requirements.txt" -t "${outputDir}"` +
                    ` --no-cache-dir --quiet --only-binary=:all:` +
                    ` --platform manylinux2014_x86_64 --python-version 3.12 --implementation cp --abi cp312` +
                    ` && cp -r "${enrichmentDir}/." "${outputDir}/"` +
                    ` && find "${outputDir}" -name __pycache__ -type d -exec rm -rf {} +`,
                  { stdio: "pipe", shell: "/bin/bash" }
                );
                return true;
              } catch {
                return false; // fall back to Docker bundling
              }
            },
          },
        },
      }),
      // The expediente pipeline OCRs scanned pages via Textract and runs
      // several Bedrock extraction calls: an 86-page expediente takes ~6 min.
      timeout: cdk.Duration.minutes(15),
      memorySize: 1024,
      description:
        "Config-driven document validation (JSON + DynamoDB + custom Node.js)",
      environment: {
        ...(catalogTable && { CATALOG_TABLE: catalogTable.tableName }),
        CATALOG_FORCE_RELOAD: forceReload ? "true" : "false",
        // Base model for the enrichment pipeline's own steps. All three are
        // text-only (they run on Textract OCR / native PDF text, never on
        // images), so a text-only model works here even when the vision steps
        // above need a multimodal one.
        BEDROCK_MODEL_ID: bedrockModel,
        // Per-step overrides, each falling back to the base above:
        //   segmenter — splits an expediente into typed page ranges
        //   extractor — the field extraction that carries most of the tokens
        //   recovery  — Textract+model re-extraction when a catalog entry sets
        //               extraction_strategy
        ...(process.env.BEDROCK_SEGMENTER_MODEL_ID && {
          BEDROCK_SEGMENTER_MODEL_ID: process.env.BEDROCK_SEGMENTER_MODEL_ID,
        }),
        ...(process.env.BEDROCK_EXTRACTOR_MODEL_ID && {
          BEDROCK_EXTRACTOR_MODEL_ID: process.env.BEDROCK_EXTRACTOR_MODEL_ID,
        }),
        ...(process.env.BEDROCK_RECOVERY_MODEL_ID && {
          BEDROCK_RECOVERY_MODEL_ID: process.env.BEDROCK_RECOVERY_MODEL_ID,
        }),
        ...(fallbackModelsTable && {
          FALLBACK_MODELS_TABLE: "document-validation-fallback-models",
        }),
        // How many times a single Bedrock call may switch models after a
        // model-unavailable error (pipeline/model_fallback.py) before giving
        // up — independent of `enableFallbackModels`/the table above, since
        // the Python default order applies either way.
        ...(process.env.MODEL_FALLBACK_MAX_RETRIES && {
          MODEL_FALLBACK_MAX_RETRIES: process.env.MODEL_FALLBACK_MAX_RETRIES,
        }),
        // Wall-clock knobs of the expediente pipeline. They were code-only
        // defaults (pdf_pages.py / text_extractor.py / expediente.py), so the
        // deployed Lambda could never be tuned without editing Python:
        //   TEXTRACT_WORKERS — scanned pages OCR'd in parallel. Every page is
        //     OCR'd before page selection can score it (scoring needs text),
        //     so a 31-page scan pays 31 Textract calls even when 3 pages end
        //     up going to the model — measured 47s end to end at 4 workers.
        //   CHUNK_WORKERS — Bedrock extraction chunks of ONE document run in
        //     parallel. A 51-page selection is 7 chunks; at 2 workers that is
        //     4 sequential waves (measured 425s).
        //   EXTRACT_WORKERS — segments (documents) of an expediente in parallel.
        // Raise carefully: the ceiling is Bedrock/Textract throttling, which
        // is why these are env vars and not constants.
        TEXTRACT_WORKERS: process.env.TEXTRACT_WORKERS || "8",
        CHUNK_WORKERS: process.env.CHUNK_WORKERS || "4",
        ...(process.env.EXTRACT_WORKERS && {
          EXTRACT_WORKERS: process.env.EXTRACT_WORKERS,
        }),
        // Dev only: let an upload pick the model for its own request (via
        // `pnpm enviar ... --model <id>`), so trying a model doesn't need a
        // redeploy. Never set in prod — the value comes from whoever uploaded
        // the file, so it would let a caller choose the model, and with it the
        // cost and the extraction quality, on someone else's document.
        ...(isProd ? {} : { ALLOW_MODEL_OVERRIDE: "true" }),
      },
    });

    // Grant DynamoDB read access if enabled
    if (catalogTable) {
      catalogTable.grantReadData(enrichmentFn);
    }
    if (fallbackModelsTable) {
      fallbackModelsTable.grantReadData(enrichmentFn);
    }

    // Textract AnalyzeID + DetectDocumentText fallback: cuando Bedrock falla el
    // OCR (sobre todo en fotos), el enrichment llama a Textract para reextraer
    // con un motor OCR especializado y entregarle el texto normalizado a
    // Bedrock Converse. AnalyzeID y DetectDocumentText no soportan ARN a nivel
    // de recurso, por eso Resources: ["*"].
    enrichmentFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["textract:AnalyzeID", "textract:DetectDocumentText"],
        resources: ["*"],
      })
    );
    // El extractor hibrido llama Bedrock Converse desde el enrichment Lambda.
    // Misma politica amplia que aplicamos abajo a las lambdas internas del
    // pipeline para soportar inference-profile ARNs cross-region.
    enrichmentFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: [
          `arn:aws:bedrock:*::foundation-model/*`,
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
        ],
      })
    );
    enrichmentFn.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["s3:GetObject", "s3:ListBucket"],
        resources: ["*"],
      })
    );

    // Main document processing pipeline
    const pipeline = new MultiModelDocumentProcessing(this, "BedrockDocumentProcessing", {
      classificationBedrockModel: {
        useCrossRegionInference: false,
        customModelId: classificationModel,
      },
      processingBedrockModel: {
        useCrossRegionInference: false,
        customModelId: processingModel,
      },
      eventbridgeBroker: broker,
      enableObservability: true,
      metricNamespace: "bedrock-document-processing",
      metricServiceName: "extraction-workflow",
      // Must exceed the enrichment Lambda's own 15-minute timeout (below):
      // the blueprint defaults this to Duration.minutes(15) when unset,
      // which ties it to the Lambda's cap. When both are equal, a genuine
      // Lambda timeout and the state machine's own ExecutionTimedOut race —
      // ExecutionTimedOut usually wins, killing the whole execution before
      // the enrichment step's addCatch can run, so the file never moves out
      // of raw/ into failed/ and the item is never marked enrichment-failure.
      // The extra minute gives the Lambda's timeout time to surface as a
      // catchable task failure instead.
      workflowTimeout: cdk.Duration.minutes(16),
      classificationPrompt,
      processingPrompt,
      enrichmentLambdaFunction: enrichmentFn,
      // Prod shares one CMK across the whole construct. Dev uses service-owned
      // encryption and must synthesize zero AWS::KMS::Key resources.
      useCustomerManagedEncryption: isProd,
      encryptionKey,
      ...(encryptionKey && {
        logGroupDataProtection: { logGroupEncryptionKey: encryptionKey },
      }),
    });

    // Tipo de documento como COLUMNA INDEXADA.
    //
    // El workflow solo guarda `EnrichmentResult` (el envelope entero como JSON
    // string), así que filtrar por tipo obligaba a un Scan completo de la tabla
    // + parseo en la Lambda. El enrichment Lambda promueve ahora el tipo final
    // (el que dictamina el segmentador, no la clasificación de visión de
    // upstream) a atributos de primer nivel — ver `enrichment-lambda/indexing.py` —
    // y este GSI los hace consultables con Query en vez de Scan.
    //
    // PK DocumentType + SK ProcessedAt → "todos los X, del más reciente al más
    // viejo" es una sola Query. La proyección INCLUDE lleva los atributos que
    // permiten filtrar del lado de DynamoDB (estado de validación y los tipos
    // contenidos en un expediente) sin traerse el envelope completo; el envelope
    // solo se lee (BatchGet) para los items que sobreviven al filtro.
    const documentTable = pipeline.documentProcessingTable;
    documentTable.addGlobalSecondaryIndex({
      indexName: DOCUMENT_TYPE_INDEX,
      partitionKey: {
        name: "DocumentType",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: { name: "ProcessedAt", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: [
        "DocumentTypeName",
        "ValidationStatus",
        "ContainedDocumentTypes",
        "DocumentName",
      ],
    });

    // El enrichment Lambda escribe esas columnas (UpdateItem) al terminar.
    enrichmentFn.addEnvironment("DOCUMENT_TABLE", documentTable.tableName);
    documentTable.grantWriteData(enrichmentFn);

    // The blueprint owns WorkflowStatus and exposes implementation-specific
    // values. This projection creates the small, stable lifecycle clients use
    // and marks the terminal state only after the file move and final event.
    const statusEventFn = new lambda.Function(this, "ProcessingStatusEventLambda", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "document-processing/resources/status-event-lambda"),
        { exclude: ["__pycache__", "**/__pycache__", "*.pyc"] }
      ),
      timeout: cdk.Duration.seconds(15),
      memorySize: 128,
      description: "Projects terminal pipeline events to public processing status",
      environment: { DOCUMENT_TABLE: documentTable.tableName },
    });
    documentTable.grant(statusEventFn, "dynamodb:UpdateItem");
    new events.Rule(this, "ProcessingStatusTerminalRule", {
      eventBus: broker.eventbus,
      eventPattern: {
        source: ["document-validation-pipeline"],
        detailType: [
          "document-processed-successful",
          "document-processing-failed",
        ],
      },
      targets: [new eventTargets.LambdaFunction(statusEventFn)],
    });

    // Tabla de UNIQUE KEYS: TIPODOC#valor -> documento.
    //
    // Un tipo puede declarar en su schema (`catalogs/*.json`) el campo cuyo
    // valor identifica UN documento de ese tipo por sí solo — el número de
    // pedimento, el UUID de un CFDI. El enrichment Lambda escribe una fila por
    // cada valor encontrado (`enrichment-lambda/unique_keys.py`) con clave de
    // partición `TIPODOC#<valor normalizado>`, así `GET /documents?docType=
    // &field=&value=` se resuelve con UNA Query en vez de recorrer todos los
    // documentos del tipo y comparar dentro del Lambda.
    //
    // Tabla aparte —y no otro atributo del item de documento— porque un
    // expediente es UN item con N documentos dentro, cada uno con su propio
    // tipo y su propia unique key: una PK de GSI solo puede llevar un valor
    // por item. `DocumentId` es SORT key, no parte de la partición: el mismo
    // identificador de negocio aparece legítimamente en más de una subida
    // (reenvío, expediente corregido) y colapsarlas en una sola fila perdería
    // todas menos la última.
    //
    // DESTROY (a diferencia de costTable, que es RETAIN): esto es un índice
    // derivado, no historia — se reconstruye entero desde los
    // `EnrichmentResult` ya guardados con `pnpm backfill:keys`.
    const uniqueKeyTable = new dynamodb.Table(this, "UniqueKeyTable", {
      tableName: `document-validation-keys${envSuffix}`,
      partitionKey: { name: "UniqueKey", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "DocumentId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      ...(encryptionKey
        ? {
            encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryptionKey,
          }
        : { encryption: dynamodb.TableEncryption.DEFAULT }),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    enrichmentFn.addEnvironment("UNIQUE_KEY_TABLE", uniqueKeyTable.tableName);
    uniqueKeyTable.grantWriteData(enrichmentFn);

    // Cost audit table: one item per processed document, written by
    // `enrichment-lambda/cost_audit.py`, with a $ breakdown per AWS service
    // actually used (Bedrock segmentation/extraction/upstream classification,
    // Textract OCR, an approximate Lambda compute cost) — see
    // `documentacion/COSTOS.md`. Deliberately a SEPARATE table from
    // `documentTable` above: this is a finance/audit concern (query by
    // DocumentId, aggregate totals via `pnpm costos`), not a document-query
    // concern, and mixing the two would mean every document-search Scan also
    // drags cost data along. RETAIN, not DESTROY like appsTable below: audit
    // history is exactly the kind of data a `pnpm run destroy` during dev
    // iteration shouldn't be able to silently erase.
    const costTable = new dynamodb.Table(this, "CostTable", {
      tableName: `document-validation-costs${envSuffix}`,
      partitionKey: { name: "DocumentId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      ...(encryptionKey
        ? {
            encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryptionKey,
          }
        : { encryption: dynamodb.TableEncryption.DEFAULT }),
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    enrichmentFn.addEnvironment("COST_TABLE", costTable.tableName);
    costTable.grantWriteData(enrichmentFn);

    // Grant broad Bedrock invoke permissions to all Lambda roles in the pipeline
    // The construct creates its own roles but customModelId with cross-region prefix
    // may not generate correct IAM ARNs for inference profiles
    const bedrockPolicy = new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
      ],
      resources: [
        `arn:aws:bedrock:*::foundation-model/*`,
        `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
      ],
    });

    // Apply to all Lambda functions created by the construct
    pipeline.node.findAll().forEach((child) => {
      if (child instanceof lambda.Function) {
        child.addToRolePolicy(bedrockPolicy);
      }
    });

    // The blueprint's QueuedS3Adapter creates the ingestion bucket internally
    // (raw/ prefix triggers the workflow). Locate it in the construct tree so
    // the Upload Lambda writes to the real bucket instead of a placeholder.
    const documentBucket = pipeline.node
      .findAll()
      .find((c): c is s3.Bucket => c instanceof s3.Bucket);
    if (!documentBucket) {
      throw new Error(
        "BedrockDocumentProcessing no longer exposes an S3 bucket in its construct tree; wire DOCUMENT_BUCKET explicitly."
      );
    }

    // The adapter bucket uses the shared CMK in prod and SSE-S3 in dev.
    // grantReadWrite automatically adds KMS permissions only in prod — the
    // expediente pipeline reads raw documents and persists complete results
    // under results/ (Step Functions payloads only carry a compact summary).
    documentBucket.grantReadWrite(enrichmentFn);

    // Apps/API keys registry: `pnpm apps:register`/`list`/`revoke` (see
    // scripts/manage-apps.mjs) create real API Gateway ApiKey+UsagePlan
    // resources directly against this deployed stack (no redeploy per app)
    // and record them here so the Upload Lambda can resolve an incoming
    // `x-api-key` back to a human-readable app name for traceability
    // (`appOrigin` on the result envelope). One table per stack — dev/prod
    // each have their own RestApi, so their ApiKeyIds are never comparable
    // across environments, unlike the shared catalog table above (DESTROY,
    // not RETAIN: this is operational metadata tied to this stack's own
    // ApiKey/UsagePlan resources, not domain data worth preserving past it).
    // PK = ApiKeyId (not AppName): the Upload Lambda needs a direct GetItem
    // on the hot path of every upload, not a Scan.
    const appsTable = new dynamodb.Table(this, "AppsTable", {
      tableName: `document-validation-apps${envSuffix}`,
      partitionKey: { name: "ApiKeyId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      ...(encryptionKey
        ? {
            encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryptionKey,
          }
        : { encryption: dynamodb.TableEncryption.DEFAULT }),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // The SQS consumer (patched via patches/default-sqs-consumer-index.py)
    // reads the uploaded object's `app-origin` S3 metadata so appOrigin
    // reaches every document regardless of which pipeline branch handles it
    // — its default IAM role only has states:StartExecution + logs, so it
    // needs S3 read granted explicitly. Same construct-tree lookup pattern
    // already used above for the ingestion bucket.
    const sqsConsumerFn = pipeline.node
      .findAll()
      .find(
        (c): c is lambda.Function =>
          c instanceof lambda.Function && c.node.id === "SQSConsumer"
      );
    if (!sqsConsumerFn) {
      throw new Error(
        "BedrockDocumentProcessing no longer exposes a 'SQSConsumer' Lambda in its construct tree; appOrigin metadata could not be read, and the app-origin propagation patch would silently do nothing."
      );
    }
    documentBucket.grantRead(sqsConsumerFn);
    sqsConsumerFn.addEnvironment("DOCUMENT_TABLE", documentTable.tableName);
    documentTable.grant(sqsConsumerFn, "dynamodb:UpdateItem");

    // Cost guardrail: catch a runaway enrichment Lambda (unexpected request
    // volume, or a single invocation crawling toward its 15-minute cap) fast,
    // instead of finding out from the AWS bill. Complements the account-wide
    // monthly Budget with something specific to this pipeline and much
    // faster to fire (minutes, not a % of a monthly ceiling).
    const alertsTopic = new sns.Topic(this, "PipelineAlertsTopic", {
      topicName: `document-validation-alerts${envSuffix}`,
      masterKey: encryptionKey,
    });
    alertsTopic.addSubscription(
      new subscriptions.EmailSubscription("luis.hernandez@alsuper.com")
    );

    // Many invocations in a short window: normal single-document testing
    // never approaches this; a real spike (script loop, retry storm, abuse)
    // does. 15-minute period so a burst of manual tests doesn't false-alarm.
    new cloudwatch.Alarm(this, "EnrichmentInvocationSpikeAlarm", {
      alarmName: `enrichment-invocation-spike${envSuffix}`,
      alarmDescription:
        "Unusually many enrichment Lambda invocations in 15 minutes — possible request storm or retry loop burning Textract/Bedrock spend.",
      metric: enrichmentFn.metricInvocations({
        period: cdk.Duration.minutes(15),
        statistic: "sum",
      }),
      threshold: 20,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(new cwActions.SnsAction(alertsTopic));

    // A single invocation crawling toward its own 15-minute hard cap: fires
    // before the timeout actually happens, so there's a chance to intervene
    // (or at least know) instead of just seeing it in the bill/logs after.
    new cloudwatch.Alarm(this, "EnrichmentDurationNearTimeoutAlarm", {
      alarmName: `enrichment-duration-near-timeout${envSuffix}`,
      alarmDescription:
        "Enrichment Lambda invocation running close to its 15-minute timeout — likely to time out and waste the full invocation's Textract/Bedrock spend.",
      metric: enrichmentFn.metricDuration({
        period: cdk.Duration.minutes(5),
        statistic: "Maximum",
      }),
      threshold: cdk.Duration.minutes(13).toMilliseconds(),
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(new cwActions.SnsAction(alertsTopic));

    // Upload Lambda - Node.js document ingestion API
    // Bundling: @aws-sdk/s3-request-presigner (used for the large-file
    // presigned-upload path) is not reliably part of the Node.js 20.x
    // managed runtime's bundled SDK the way @aws-sdk/client-* packages are,
    // so it's installed explicitly here — same local-npm-then-Docker-fallback
    // pattern as the enrichment Lambda's pip bundling below.
    const uploadDir = path.join(
      __dirname,
      "document-processing/resources/upload-lambda"
    );
    const uploadFn = new lambda.Function(this, "UploadLambda", {
      runtime: lambda.Runtime.NODEJS_20_X,
      handler: "index.handler",
      code: lambda.Code.fromAsset(uploadDir, {
        exclude: ["node_modules"],
        bundling: {
          image: lambda.Runtime.NODEJS_20_X.bundlingImage,
          command: [
            "bash",
            "-c",
            "cp -au . /asset-output && cd /asset-output && npm install --omit=dev --no-audit --no-fund --quiet",
          ],
          local: {
            tryBundle(outputDir: string): boolean {
              const { execSync } = require("child_process");
              try {
                execSync(
                  `cp -r "${uploadDir}/." "${outputDir}/"` +
                    ` && cd "${outputDir}" && npm install --omit=dev --no-audit --no-fund --quiet`,
                  { stdio: "pipe", shell: "/bin/bash" }
                );
                return true;
              } catch {
                return false; // fall back to Docker bundling
              }
            },
          },
        },
      }),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      description: "Document upload via REST API",
      environment: {
        DOCUMENT_BUCKET: documentBucket.bucketName,
        APPS_TABLE: appsTable.tableName,
        DOCUMENT_TABLE: documentTable.tableName,
      },
    });
    documentBucket.grantPut(uploadFn);
    appsTable.grantReadData(uploadFn);
    documentTable.grant(uploadFn, "dynamodb:PutItem");

    // Query Lambda - read/search documents, patch caller-owned metadata and
    // reprocess an existing source object under a new immutable document id.
    const queryFn = new lambda.Function(this, "QueryLambda", {
      runtime: lambda.Runtime.NODEJS_20_X,
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "document-processing/resources/query-lambda")
      ),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      description: "Document read/search, metadata patch and reprocess via REST API",
      environment: {
        DOCUMENT_TABLE: documentTable.tableName,
        DOCUMENT_BUCKET: documentBucket.bucketName,
        // Query por tipo (docType) contra el GSI en vez de Scan.
        DOCUMENT_TYPE_INDEX,
        // Resuelve nombres/alias de tipo ("pedimento aduanal") al TYPE_ID
        // canónico usando el catálogo hot-reload cuando está habilitado.
        ...(catalogTable && { CATALOG_TABLE: catalogTable.tableName }),
      },
    });
    // GetItem + Query/Scan on the tracking table and its GSI (and KMS decrypt
    // for its CMK) — grantReadData covers `${tableArn}/index/*` — plus read of
    // the full results/ objects in S3.
    documentTable.grantReadData(queryFn);
    // PATCH updates metadata; POST /documents/{id}/reprocess reserves a new
    // job row and marks it FAILED if its internal S3 copy cannot be created.
    documentTable.grant(queryFn, "dynamodb:PutItem", "dynamodb:UpdateItem");
    // Reprocess reads processed/failed and writes a server-side copy to raw/.
    // Keep DeleteObject out of this Lambda: CopyObject only needs source read
    // + destination put. The two grants also carry matching KMS permissions.
    documentBucket.grantRead(queryFn);
    documentBucket.grantPut(queryFn);
    // Camino rápido de búsqueda: si el campo consultado es la unique key
    // declarada por el tipo, una sola Query aquí sustituye al recorrido
    // completo del tipo (ver `unique_keys.py` y el bloque UniqueKeyTable).
    queryFn.addEnvironment("UNIQUE_KEY_TABLE", uniqueKeyTable.tableName);
    uniqueKeyTable.grantReadData(queryFn);
    if (catalogTable) {
      catalogTable.grantReadData(queryFn);
    }
    // So the Query Lambda can resolve the caller's own app (from its
    // x-api-key) and enforce per-app document access (AccessScope/
    // AllowedDocTypes/AllowFieldSearch/AllowMetadataWrite — see
    // manage-apps.mjs) instead of any
    // valid key being able to read any app's documents.
    queryFn.addEnvironment("APPS_TABLE", appsTable.tableName);
    appsTable.grantReadData(queryFn);

    // API Gateway for document upload
    const api = new apigateway.RestApi(this, "DocumentUploadApi", {
      restApiName: "Document Upload API",
      description:
        "REST API for uploading documents to the validation pipeline",
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: ["GET", "POST", "PATCH", "OPTIONS"],
      },
      // Dev has no API key / WAF (open on purpose, for easy testing — see
      // methodOptions below), so without a stage-wide default it has no rate
      // limit at all: a bug or accidental script loop could fire uploads as
      // fast as the client can send them, each one billing Textract+Bedrock.
      // This is a blunt, key-independent cap — every caller shares it, unlike
      // prod's per-API-key usage plan below. Prod already has WAF + a usage
      // plan, so this only applies to dev.
      ...(!isProd && {
        deployOptions: {
          throttlingRateLimit: 10,
          throttlingBurstLimit: 20,
        },
      }),
    });

    // In prod every method requires an API key: requests without a valid
    // `x-api-key` get a native 403 from API Gateway WITHOUT invoking the
    // backend Lambda — so 401/403/404 never consume Lambda/Bedrock. In dev the
    // API stays open for easy testing, unless devApiKeyRequired opts it into
    // the same enforcement (per-app keys still come from `pnpm apps:register`
    // in both cases, never from the CDK-managed idp-default block below).
    const requireApiKey = isProd || (props?.devApiKeyRequired ?? false);
    const methodOptions: apigateway.MethodOptions = { apiKeyRequired: requireApiKey };

    // POST /documents                            → create a document; returns the presigned upload URL
    // GET  /documents?field=<dot.path>&value=<v>  → search by any extracted field
    // GET  /documents/{id}                        → full result for one document
    // GET  /documents/{id}/file                   → presigned S3 URL to the original uploaded file
    // POST /documents/{id}/reprocess              → new job from the original S3 object
    // PATCH /documents/{id}/metadata              → merge external metadata (not validation status)
    const uploadIntegration = new apigateway.LambdaIntegration(uploadFn);
    const queryIntegration = new apigateway.LambdaIntegration(queryFn);
    const documentsResource = api.root.addResource("documents");
    documentsResource.addMethod("POST", uploadIntegration, methodOptions);
    documentsResource.addMethod("GET", queryIntegration, methodOptions);
    const documentByIdResource = documentsResource.addResource("{id}");
    documentByIdResource.addMethod("GET", queryIntegration, methodOptions);
    documentByIdResource.addResource("file").addMethod("GET", queryIntegration, methodOptions);
    documentByIdResource.addResource("reprocess").addMethod("POST", queryIntegration, methodOptions);
    documentByIdResource.addResource("metadata").addMethod("PATCH", queryIntegration, methodOptions);

    // Alias NO DOCUMENTADO de `POST /documents`, a propósito: la documentación
    // (openapi.yaml, API_USO.md) describe únicamente `/documents`, y esta ruta
    // existe solo para que las integraciones que ya usaban `/upload` sigan
    // funcionando. El nombre dejó de ser cierto cuando el endpoint dejó de
    // recibir el archivo. Misma Lambda, misma respuesta. Retirarlo es borrar
    // estas dos líneas — cambio incompatible, avisar antes.
    api.root.addResource("upload").addMethod("POST", uploadIntegration, methodOptions);

    // ---- Prod hardening: API keys + usage plan, WAF, custom domain ----------
    if (isProd) {
      const throttle = {
        rateLimit: props?.apiThrottle?.rateLimit ?? 20,
        burstLimit: props?.apiThrottle?.burstLimit ?? 40,
      };
      const quotaPerDay = props?.apiThrottle?.quotaPerDay ?? 5000;

      // Usage plan enforces rate/burst + a daily quota per key.
      const usagePlan = api.addUsagePlan("DefaultUsagePlan", {
        name: "idp-default",
        throttle,
        quota: { limit: quotaPerDay, period: apigateway.Period.DAY },
      });
      usagePlan.addApiStage({ stage: api.deploymentStage });

      // One key to start; add more (one per consuming system) as needed.
      const apiKey = api.addApiKey("DefaultApiKey", { apiKeyName: "idp-default" });
      usagePlan.addApiKey(apiKey);
      new cdk.CfnOutput(this, "ApiKeyId", {
        value: apiKey.keyId,
        description:
          "API key id. Get the secret value: aws apigateway get-api-key --api-key <id> --include-value",
      });

      // WAF: rate-based anti-DDoS + AWS managed rule groups. Blocked requests
      // (403) are dropped before the integration → no Lambda cost.
      const webAcl = new wafv2.CfnWebACL(this, "ApiWebAcl", {
        defaultAction: { allow: {} },
        scope: "REGIONAL",
        visibilityConfig: {
          cloudWatchMetricsEnabled: true,
          metricName: "idp-web-acl",
          sampledRequestsEnabled: true,
        },
        rules: [
          {
            name: "RateLimitPerIp",
            priority: 0,
            action: { block: {} },
            statement: {
              rateBasedStatement: {
                limit: props?.wafRateLimit ?? 2000,
                aggregateKeyType: "IP",
              },
            },
            visibilityConfig: {
              cloudWatchMetricsEnabled: true,
              metricName: "idp-rate-limit",
              sampledRequestsEnabled: true,
            },
          },
          ...[
            "AWSManagedRulesCommonRuleSet",
            "AWSManagedRulesKnownBadInputsRuleSet",
            "AWSManagedRulesAmazonIpReputationList",
          ].map((name, i) => ({
            name,
            priority: i + 1,
            overrideAction: { none: {} },
            statement: {
              managedRuleGroupStatement: { vendorName: "AWS", name },
            },
            visibilityConfig: {
              cloudWatchMetricsEnabled: true,
              metricName: name,
              sampledRequestsEnabled: true,
            },
          })),
        ],
      });
      new wafv2.CfnWebACLAssociation(this, "ApiWebAclAssociation", {
        resourceArn: api.deploymentStage.stageArn,
        webAclArn: webAcl.attrArn,
      });

      // Custom domain (only when both domainName and certArn are configured).
      if (props?.domainName && props?.certArn) {
        const certificate = acm.Certificate.fromCertificateArn(
          this,
          "ApiCertificate",
          props.certArn
        );
        const domain = new apigateway.DomainName(this, "ApiCustomDomain", {
          domainName: props.domainName,
          certificate,
          endpointType: apigateway.EndpointType.REGIONAL,
          securityPolicy: apigateway.SecurityPolicy.TLS_1_2,
        });
        domain.addBasePathMapping(api, { stage: api.deploymentStage });
        new cdk.CfnOutput(this, "CustomDomainTarget", {
          value: domain.domainNameAliasDomainName,
          description: `Add a CNAME: ${props.domainName} -> this value (external DNS)`,
        });
      }
    }

    // Outputs
    new cdk.CfnOutput(this, "ApiEndpoint", {
      value: api.url,
      description: "Document Upload API endpoint (random execute-api URL)",
    });

    if (catalogTable) {
      new cdk.CfnOutput(this, "CatalogTableName", {
        value: catalogTable.tableName,
        description:
          "DynamoDB table for runtime document type catalog (hot-reload)",
      });
    }
  }
}
