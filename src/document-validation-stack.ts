import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
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
    const enrichmentFn = new lambda.Function(this, "EnrichmentLambda", {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(
        path.join(
          __dirname,
          "document-processing/resources/enrichment-lambda"
        )
      ),
      timeout: cdk.Duration.seconds(60),
      memorySize: 512,
      description:
        "Config-driven document validation (JSON + DynamoDB + custom Node.js)",
      environment: {
        ...(catalogTable && { CATALOG_TABLE: "document-validation-catalog" }),
        CATALOG_FORCE_RELOAD: forceReload ? "true" : "false",
      },
    });

    // Grant DynamoDB read access if enabled
    if (catalogTable) {
      catalogTable.grantReadData(enrichmentFn);
    }

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
        DOCUMENT_BUCKET: "PLACEHOLDER_BUCKET",
      },
    });

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
