import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cwActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sns from "aws-cdk-lib/aws-sns";
import * as subscriptions from "aws-cdk-lib/aws-sns-subscriptions";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import {
  BedrockDocumentProcessing,
  EventbridgeBroker,
} from "@cdklabs/cdk-appmod-catalog-blueprints";
import { Construct } from "constructs";
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
    const bedrockModel = process.env.BEDROCK_MODEL_ID || "us.anthropic.claude-haiku-4-5-20251001-v1:0";

    // Environment gate: prod hardens the API (keys + WAF + custom domain);
    // dev stays open on the random execute-api URL.
    const environment = props?.environment ?? "dev";
    const isProd = environment === "prod";
    // Suffix for account-global resource names (KMS alias, EventBus) so dev can
    // coexist with prod. Prod keeps the bare names it was first deployed with.
    const envSuffix = isProd ? "" : "-dev";

    // ONE customer-managed KMS key shared across the pipeline instead of a
    // separate CMK per component. Each CMK costs ~$1/month just to exist; the
    // blueprint otherwise creates 5 (base, log group, adapter, broker, bucket).
    // Consolidating cuts fixed KMS spend with no change to encryption at rest —
    // only that components share one customer key instead of isolated keys.
    const encryptionKey = new kms.Key(this, "PipelineEncryptionKey", {
      enableKeyRotation: true,
      alias: `document-validation/pipeline${envSuffix}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // EventBridge broker for pipeline notifications
    const broker = new EventbridgeBroker(this, "IDPBroker", {
      name: `idp-broker${envSuffix}`,
      eventSource: "document-validation-pipeline",
      kmsKey: encryptionKey,
    });

    // Generate prompts dynamically from the JSON config catalog
    const classificationPrompt = generateClassificationPrompt();
    const processingPrompt = generateProcessingPrompt();

    // Optional: DynamoDB catalog table for runtime hot-reload
    // Uses existing table if it already exists (RETAIN policy = never deleted on stack destroy)
    let catalogTable: dynamodb.ITable | undefined;
    if (enableDynamo) {
      const tableName = "document-validation-catalog";

      // Import existing table to avoid "already exists" errors on redeploy
      // The table persists independently of the stack lifecycle
      catalogTable = dynamodb.Table.fromTableName(
        this,
        "DocumentCatalogTable",
        tableName
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
        ...(catalogTable && { CATALOG_TABLE: "document-validation-catalog" }),
        CATALOG_FORCE_RELOAD: forceReload ? "true" : "false",
        // Model used by the Textract+Bedrock re-extraction fallback when a
        // catalog entry sets extraction_strategy. Defaults to the classifier
        // model when unset; override here to use a stronger vision model
        // (e.g. sonnet) only for recovery without affecting the main pipeline.
        BEDROCK_EXTRACTOR_MODEL_ID:
          process.env.BEDROCK_EXTRACTOR_MODEL_ID || bedrockModel,
      },
    });

    // Grant DynamoDB read access if enabled
    if (catalogTable) {
      catalogTable.grantReadData(enrichmentFn);
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
    const pipeline = new BedrockDocumentProcessing(this, "BedrockDocumentProcessing", {
      classificationBedrockModel: {
        useCrossRegionInference: false,
        customModelId: bedrockModel,
      },
      processingBedrockModel: {
        useCrossRegionInference: false,
        customModelId: bedrockModel,
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
      // Share the single CMK for the table + state machine, and for the log
      // group data-protection key (else the blueprint mints its own CMK each).
      encryptionKey,
      logGroupDataProtection: { logGroupEncryptionKey: encryptionKey },
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

    // The adapter's bucket is SSE-KMS encrypted with its OWN key (not
    // pipeline.encryptionKey). grantReadWrite wires s3:Get/PutObject AND the
    // KMS decrypt/encrypt on the right key — the expediente pipeline reads
    // raw documents and persists full results under results/ (Step Functions
    // payloads only carry a compact summary).
    documentBucket.grantReadWrite(enrichmentFn);

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
      },
    });
    documentBucket.grantPut(uploadFn);

    // Query Lambda - read a document by id and search by any extracted field.
    const queryFn = new lambda.Function(this, "QueryLambda", {
      runtime: lambda.Runtime.NODEJS_20_X,
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "document-processing/resources/query-lambda")
      ),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      description: "Document read/search via REST API",
      environment: {
        DOCUMENT_TABLE: documentTable.tableName,
        DOCUMENT_BUCKET: documentBucket.bucketName,
        // Query por tipo (docType) contra el GSI en vez de Scan.
        DOCUMENT_TYPE_INDEX,
        // Resuelve nombres/alias de tipo ("pedimento aduanal") al TYPE_ID
        // canónico usando el catálogo hot-reload cuando está habilitado.
        ...(catalogTable && { CATALOG_TABLE: "document-validation-catalog" }),
      },
    });
    // GetItem + Query/Scan on the tracking table and its GSI (and KMS decrypt
    // for its CMK) — grantReadData covers `${tableArn}/index/*` — plus read of
    // the full results/ objects in S3.
    documentTable.grantReadData(queryFn);
    documentBucket.grantRead(queryFn);
    if (catalogTable) {
      catalogTable.grantReadData(queryFn);
    }

    // API Gateway for document upload
    const api = new apigateway.RestApi(this, "DocumentUploadApi", {
      restApiName: "Document Upload API",
      description:
        "REST API for uploading documents to the validation pipeline",
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: ["GET", "POST", "OPTIONS"],
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
    // API stays open for easy testing.
    const methodOptions: apigateway.MethodOptions = { apiKeyRequired: isProd };

    const uploadResource = api.root.addResource("upload");
    uploadResource.addMethod(
      "POST",
      new apigateway.LambdaIntegration(uploadFn),
      methodOptions
    );

    // GET /documents?field=<dot.path>&value=<v>  → search by any extracted field
    // GET /documents/{id}                        → full result for one document
    const queryIntegration = new apigateway.LambdaIntegration(queryFn);
    const documentsResource = api.root.addResource("documents");
    documentsResource.addMethod("GET", queryIntegration, methodOptions);
    documentsResource.addResource("{id}").addMethod("GET", queryIntegration, methodOptions);

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
        value: "document-validation-catalog",
        description:
          "DynamoDB table for runtime document type catalog (hot-reload)",
      });
    }
  }
}
