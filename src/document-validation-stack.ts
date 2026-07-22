import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as s3 from "aws-cdk-lib/aws-s3";
import {
  BedrockDocumentProcessing,
  EventbridgeBroker,
} from "@cdklabs/cdk-appmod-catalog-blueprints";
import { Construct } from "constructs";
import {
  generateClassificationPrompt,
  generateProcessingPrompt,
} from "./prompt-generator";

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

    // EventBridge broker for pipeline notifications
    const broker = new EventbridgeBroker(this, "IDPBroker", {
      name: "idp-broker",
      eventSource: "document-validation-pipeline",
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
                execSync(
                  `pip3 install -r "${enrichmentDir}/requirements.txt" -t "${outputDir}" --no-cache-dir --quiet` +
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
      classificationPrompt,
      processingPrompt,
      enrichmentLambdaFunction: enrichmentFn,
    });

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

    // Upload Lambda - Node.js document ingestion API
    const uploadFn = new lambda.Function(this, "UploadLambda", {
      runtime: lambda.Runtime.NODEJS_20_X,
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "document-processing/resources/upload-lambda")
      ),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      description: "Document upload via REST API",
      environment: {
        DOCUMENT_BUCKET: documentBucket.bucketName,
      },
    });
    documentBucket.grantPut(uploadFn);

    // API Gateway for document upload
    const api = new apigateway.RestApi(this, "DocumentUploadApi", {
      restApiName: "Document Upload API",
      description:
        "REST API for uploading documents to the validation pipeline",
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: ["POST", "OPTIONS"],
      },
    });

    const uploadResource = api.root.addResource("upload");
    uploadResource.addMethod(
      "POST",
      new apigateway.LambdaIntegration(uploadFn)
    );

    // Outputs
    new cdk.CfnOutput(this, "ApiEndpoint", {
      value: api.url,
      description: "Document Upload API endpoint",
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
