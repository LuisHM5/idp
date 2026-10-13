import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as events from "aws-cdk-lib/aws-events";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sqs from "aws-cdk-lib/aws-sqs";
import {
  Choice,
  Chain,
  Condition,
  DefinitionBody,
  IChainable,
  JsonPath,
  LogLevel,
  Pass,
  Result,
  StateMachine,
  TaskInput,
} from "aws-cdk-lib/aws-stepfunctions";
import {
  CallAwsService,
  DynamoAttributeValue,
  DynamoPutItem,
  DynamoUpdateItem,
  EventBridgePutEvents,
  LambdaInvoke,
} from "aws-cdk-lib/aws-stepfunctions-tasks";
import { Construct } from "constructs";

interface ModelConfig {
  readonly customModelId?: string;
  readonly useCrossRegionInference?: boolean;
}

export interface MultiModelDocumentProcessingProps {
  readonly classificationBedrockModel?: ModelConfig;
  readonly processingBedrockModel?: ModelConfig;
  readonly classificationModelId?: string;
  readonly processingModelId?: string;
  readonly classificationPrompt?: string;
  readonly processingPrompt?: string;
  readonly enrichmentLambdaFunction: lambda.IFunction;
  readonly eventBus: events.IEventBus;
  readonly catalogTable?: dynamodb.ITable;
  readonly eventSource?: string;
  readonly enableObservability?: boolean;
  readonly metricNamespace?: string;
  readonly metricServiceName?: string;
  readonly workflowTimeout?: cdk.Duration;
  readonly stepTimeouts?: cdk.Duration;
  readonly maxDocumentBytes?: number;
  readonly largeDocFallbackType?: string;
  readonly useStructuredOutput?: boolean;
  readonly useCustomerManagedEncryption?: boolean;
  readonly encryptionKey?: kms.IKey;
  readonly removalPolicy?: cdk.RemovalPolicy;
}

/**
 * Project-owned S3 → SQS → Step Functions document pipeline.
 *
 * Historical construct ids are preserved so CloudFormation updates the
 * deployed stateful resources in place while the application stops depending
 * on implementation details patched inside catalog-blueprints.
 */
export class MultiModelDocumentProcessing extends Construct {
  readonly documentProcessingTable: dynamodb.Table;
  readonly documentBucket: s3.Bucket;
  readonly documentQueue: sqs.Queue;
  readonly deadLetterQueue: sqs.Queue;
  readonly sqsConsumerFunction: lambda.Function;
  readonly stateMachine: StateMachine;
  readonly metricNamespace: string;
  readonly metricServiceName: string;

  private readonly props: MultiModelDocumentProcessingProps;
  private classificationFn?: lambda.Function;
  private processingFn?: lambda.Function;
  private classificationSteps = 0;
  private processingSteps = 0;

  constructor(scope: Construct, id: string, props: MultiModelDocumentProcessingProps) {
    super(scope, id);
    this.props = props;
    this.metricNamespace = props.metricNamespace ?? "bedrock-document-processing";
    this.metricServiceName = props.metricServiceName ?? "extraction-workflow";

    const useKms = props.useCustomerManagedEncryption ?? Boolean(props.encryptionKey);
    if (useKms && !props.encryptionKey) {
      throw new Error("Customer-managed encryption requires encryptionKey");
    }
    if (!useKms && props.encryptionKey) {
      throw new Error("encryptionKey cannot be supplied when customer-managed encryption is disabled");
    }
    const removalPolicy = props.removalPolicy ?? cdk.RemovalPolicy.DESTROY;

    this.documentBucket = new s3.Bucket(this, "DocumentProcessingBucket", {
      autoDeleteObjects: removalPolicy === cdk.RemovalPolicy.DESTROY,
      removalPolicy,
      encryption: useKms ? s3.BucketEncryption.KMS : s3.BucketEncryption.S3_MANAGED,
      encryptionKey: props.encryptionKey,
      enforceSSL: true,
      bucketKeyEnabled: useKms,
    });
    this.deadLetterQueue = new sqs.Queue(this, "DocumentProcessingDLQ", {
      visibilityTimeout: cdk.Duration.seconds(300),
      removalPolicy,
      enforceSSL: true,
      encryption: useKms ? sqs.QueueEncryption.KMS : sqs.QueueEncryption.SQS_MANAGED,
      encryptionMasterKey: props.encryptionKey,
    });
    this.documentQueue = new sqs.Queue(this, "DocumentProcessingQueue", {
      visibilityTimeout: cdk.Duration.seconds(300),
      removalPolicy,
      enforceSSL: true,
      deadLetterQueue: { maxReceiveCount: 5, queue: this.deadLetterQueue },
      encryption: useKms ? sqs.QueueEncryption.KMS : sqs.QueueEncryption.SQS_MANAGED,
      encryptionMasterKey: props.encryptionKey,
    });
    this.documentProcessingTable = new dynamodb.Table(this, "DocumentProcessingTable", {
      partitionKey: { name: "DocumentId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: useKms
        ? dynamodb.TableEncryption.CUSTOMER_MANAGED
        : dynamodb.TableEncryption.DEFAULT,
      encryptionKey: props.encryptionKey,
      removalPolicy,
    });

    const stateMachineLogGroup = props.enableObservability
      ? new logs.LogGroup(this, "bedrock-document-processing-workflow-LogGroup", {
          logGroupName: `/aws/sfn-statemachine/${cdk.Names.uniqueResourceName(this, {
            maxLength: 100,
          })}-statemachine`,
          retention: logs.RetentionDays.TWO_YEARS,
          encryptionKey: props.encryptionKey,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        })
      : undefined;
    this.stateMachine = new StateMachine(this, "bedrock-document-processing-workflow", {
      definitionBody: DefinitionBody.fromChainable(this.createWorkflow()),
      timeout: props.workflowTimeout ?? cdk.Duration.minutes(16),
      tracingEnabled: props.enableObservability,
      ...(stateMachineLogGroup && {
        logs: { destination: stateMachineLogGroup, level: LogLevel.ALL },
      }),
      encryptionConfiguration: props.encryptionKey
        ? new cdk.aws_stepfunctions.CustomerManagedEncryptionConfiguration(props.encryptionKey)
        : new cdk.aws_stepfunctions.AwsOwnedEncryptionConfiguration(),
    });
    // CopyObject is authorized by S3 as a read of the source plus a write of
    // the destination; the service-integration action alone is not enough.
    // Using the bucket grant also adds the shared KMS permissions in prod.
    this.documentBucket.grantReadWrite(this.stateMachine.role);

    const consumerName = this.uniqueFunctionName("SQSConsumer");
    const consumerLogGroup = this.lambdaLogGroup("SQSConsumer", consumerName);
    this.sqsConsumerFunction = new lambda.Function(this, "SQSConsumer", {
      functionName: consumerName,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "resources/sqs-consumer"), {
        exclude: ["__pycache__", "*.pyc"],
      }),
      timeout: cdk.Duration.minutes(5),
      description: "Consumes SQS messages and triggers Step Functions executions for document processing",
      logGroup: consumerLogGroup,
      tracing: props.enableObservability ? lambda.Tracing.ACTIVE : lambda.Tracing.DISABLED,
      environmentEncryption: props.encryptionKey,
      environment: {
        STATE_MACHINE_ARN: this.stateMachine.stateMachineArn,
        RAW_PREFIX: "raw/",
        DOCUMENT_TABLE: this.documentProcessingTable.tableName,
        POWERTOOLS_METRICS_NAMESPACE: this.metricNamespace,
        POWERTOOLS_SERVICE_NAME: this.metricServiceName,
        LOG_LEVEL: "INFO",
        ...(props.catalogTable && { CATALOG_TABLE: props.catalogTable.tableName }),
      },
    });
    this.stateMachine.grantStartExecution(this.sqsConsumerFunction);
    this.documentBucket.grantRead(this.sqsConsumerFunction);
    this.documentProcessingTable.grant(this.sqsConsumerFunction, "dynamodb:UpdateItem");
    props.catalogTable?.grantReadData(this.sqsConsumerFunction);
    this.sqsConsumerFunction.addEventSource(
      new lambdaEventSources.SqsEventSource(this.documentQueue, {
        batchSize: 10,
        maxBatchingWindow: cdk.Duration.seconds(0),
        reportBatchItemFailures: true,
      })
    );
    this.addS3Notification();
  }

  private createWorkflow(): IChainable {
    const init = new DynamoPutItem(this, "InitMetadataEntry", {
      table: this.documentProcessingTable,
      item: {
        DocumentId: DynamoAttributeValue.fromString(JsonPath.stringAt("$.documentId")),
        ContentType: DynamoAttributeValue.fromString(JsonPath.stringAt("$.contentType")),
        Content: DynamoAttributeValue.fromString(
          JsonPath.jsonToString(JsonPath.objectAt("$.content"))
        ),
        WorkflowStatus: DynamoAttributeValue.fromString("pending"),
        StateMachineExecId: DynamoAttributeValue.fromString(
          JsonPath.stringAt("$$.Execution.Id")
        ),
        ProcessingStatus: DynamoAttributeValue.fromString("PROCESSING"),
        ProcessingStatusUpdatedAt: DynamoAttributeValue.fromString(
          JsonPath.stringAt("$.eventTime")
        ),
        JobOwner: DynamoAttributeValue.fromString(JsonPath.stringAt("$.jobOwner")),
        RequestedDocumentType: DynamoAttributeValue.fromString(
          JsonPath.stringAt("$.requestedDocumentType")
        ),
        RegisteredAt: DynamoAttributeValue.fromString(JsonPath.stringAt("$.registeredAt")),
        UploadExpiresAt: DynamoAttributeValue.fromString(
          JsonPath.stringAt("$.uploadExpiresAt")
        ),
      },
      resultPath: JsonPath.DISCARD,
    });
    return init.next(
      new Choice(this, "SelectCatalogPipelineRoute")
        .when(
          Condition.stringEquals("$.pipelineRoute", "enrichment-only"),
          this.createEnrichmentOnlyWorkflow()
        )
        .otherwise(this.createStandardWorkflow())
    );
  }

  private createStandardWorkflow(): IChainable {
    // Keep the historical state names on the standard branch so operational
    // dashboards and execution traces remain recognizable after migration.
    const moveFailed = this.moveChain("failed");
    moveFailed.next(
      this.eventTask(
        "SfnEbPutEventChain-document-processing-failed",
        "document-processing-failed",
        false
      )
    );
    const moveProcessed = this.moveChain("processed");
    moveProcessed.next(
      this.eventTask(
        "SfnEbPutEventChain-document-processed-successful",
        "document-processed-successful",
        true
      )
    );

    const classification = this.classificationStep();
    classification.addCatch(
      this.failureStatus("ClassificationFailDDBUpdate", "classification-failure").next(
        moveFailed
      ),
      { resultPath: JsonPath.DISCARD }
    );
    const classificationSaved = new DynamoUpdateItem(
      this,
      "ClassificationSuccessUpdate",
      {
        table: this.documentProcessingTable,
        key: {
          DocumentId: DynamoAttributeValue.fromString(JsonPath.stringAt("$.documentId")),
        },
        updateExpression: "SET WorkflowStatus = :status, ClassificationResult = :result",
        expressionAttributeValues: {
          ":status": DynamoAttributeValue.fromString("classification-complete"),
          ":result": DynamoAttributeValue.fromString(
            JsonPath.jsonToString(JsonPath.objectAt("$.classificationResult"))
          ),
        },
        resultPath: JsonPath.DISCARD,
      }
    );

    const processing = this.processingStep();
    processing.addCatch(
      this.failureStatus("ProcessingFailDDBUpdate", "processing-failure").next(
        moveFailed
      ),
      { resultPath: JsonPath.DISCARD }
    );
    const processingSaved = new DynamoUpdateItem(this, "ProcessingSuccessUpdate", {
      table: this.documentProcessingTable,
      key: {
        DocumentId: DynamoAttributeValue.fromString(JsonPath.stringAt("$.documentId")),
      },
      updateExpression: "SET WorkflowStatus = :status, ProcessingResult = :result",
      expressionAttributeValues: {
        ":status": DynamoAttributeValue.fromString("processing-complete"),
        ":result": DynamoAttributeValue.fromString(
          JsonPath.jsonToString(JsonPath.objectAt("$.processingResult"))
        ),
      },
      resultPath: JsonPath.DISCARD,
    });

    const enrichment = this.enrichmentStep("EnrichmentStep-0");
    enrichment.addCatch(
      this.failureStatus("EnrichmentFailDDBUpdate", "enrichment-failure").next(
        moveFailed
      ),
      { resultPath: JsonPath.DISCARD }
    );
    classification.next(classificationSaved.next(processing));
    processing.next(processingSaved.next(enrichment));
    enrichment.next(
      this.enrichmentSuccess("EnrichmentSuccessUpdate").next(moveProcessed)
    );
    return classification;
  }

  private createEnrichmentOnlyWorkflow(): IChainable {
    const prepareClassification = new Pass(this, "PrepareDeclaredClassification", {
      parameters: { "documentClassification.$": "$.requestedDocumentType" },
      resultPath: "$.classificationResult",
    });
    const prepareProcessing = new Pass(this, "PrepareEmptyUpstreamExtraction", {
      result: Result.fromObject({
        documentClassification: null,
        result: { entities: [] },
        modelId: null,
        usage: {},
      }),
      resultPath: "$.processingResult",
    });
    const enrichment = this.enrichmentStep("EnrichmentStep-1");
    const moveFailed = this.moveChain("failed", "EnrichmentOnly");
    moveFailed.next(
      this.eventTask("EnrichmentOnlyFailedEvent", "document-processing-failed", false)
    );
    enrichment.addCatch(
      this.failureStatus("EnrichmentOnlyFailureStatus", "enrichment-failure").next(
        moveFailed
      ),
      { resultPath: JsonPath.DISCARD }
    );
    const moveProcessed = this.moveChain("processed", "EnrichmentOnly");
    moveProcessed.next(
      this.eventTask(
        "EnrichmentOnlySuccessEvent",
        "document-processed-successful",
        true
      )
    );
    enrichment.next(
      this.enrichmentSuccess("EnrichmentOnlySuccessStatus").next(moveProcessed)
    );
    return prepareClassification.next(prepareProcessing).next(enrichment);
  }

  private enrichmentStep(id: string): LambdaInvoke {
    return new LambdaInvoke(this, id, {
      lambdaFunction: this.props.enrichmentLambdaFunction,
      resultPath: "$.enrichedResult",
      outputPath: "$",
      // Preserve the historical contract: downstream DynamoDB stores the
      // canonical JSON returned by handler.py, not the Lambda service envelope.
      payloadResponseOnly: true,
    });
  }

  private enrichmentSuccess(id: string): DynamoUpdateItem {
    return new DynamoUpdateItem(this, id, {
      table: this.documentProcessingTable,
      key: {
        DocumentId: DynamoAttributeValue.fromString(JsonPath.stringAt("$.documentId")),
      },
      updateExpression: "SET WorkflowStatus = :status, EnrichmentResult = :result",
      expressionAttributeValues: {
        ":status": DynamoAttributeValue.fromString("complete"),
        ":result": DynamoAttributeValue.fromString(
          JsonPath.jsonToString(JsonPath.objectAt("$.enrichedResult"))
        ),
      },
      resultPath: JsonPath.DISCARD,
    });
  }

  private failureStatus(id: string, status: string): DynamoUpdateItem {
    return new DynamoUpdateItem(this, id, {
      table: this.documentProcessingTable,
      key: {
        DocumentId: DynamoAttributeValue.fromString(JsonPath.stringAt("$.documentId")),
      },
      updateExpression: "SET WorkflowStatus = :status",
      expressionAttributeValues: { ":status": DynamoAttributeValue.fromString(status) },
      resultPath: JsonPath.DISCARD,
    });
  }

  private moveChain(destination: "processed" | "failed", idPrefix?: string): Chain {
    const label = destination === "processed" ? "Processed" : "Failed";
    const prefix = idPrefix ? `${idPrefix}-` : "";
    const copy = new CallAwsService(this, `${prefix}CopyTo${label}`, {
      service: "s3",
      action: "copyObject",
      parameters: {
        Bucket: JsonPath.stringAt("$.content.bucket"),
        CopySource: JsonPath.format(
          "{}/{}",
          JsonPath.stringAt("$.content.bucket"),
          JsonPath.stringAt("$.content.key")
        ),
        Key: JsonPath.format(
          `${destination}/{}/{}`,
          JsonPath.stringAt("$.documentId"),
          JsonPath.stringAt("$.content.filename")
        ),
      },
      iamResources: [`${this.documentBucket.bucketArn}/*`],
      resultPath: JsonPath.DISCARD,
    });
    const deleteLabel = destination === "processed" ? "DeleteFromRawSuccess" : "DeleteFromRaw";
    const remove = new CallAwsService(this, `${prefix}${deleteLabel}`, {
      service: "s3",
      action: "deleteObject",
      parameters: {
        Bucket: JsonPath.stringAt("$.content.bucket"),
        Key: JsonPath.stringAt("$.content.key"),
      },
      iamResources: [`${this.documentBucket.bucketArn}/*`],
      resultPath: JsonPath.DISCARD,
    });
    return copy.next(remove);
  }

  private eventTask(
    id: string,
    detailType: string,
    includeClassification: boolean
  ): EventBridgePutEvents {
    return new EventBridgePutEvents(this, id, {
      entries: [
        {
          eventBus: this.props.eventBus,
          source: this.props.eventSource ?? "document-validation-pipeline",
          detailType,
          detail: TaskInput.fromObject({
            documentId: JsonPath.stringAt("$.documentId"),
            contentType: JsonPath.stringAt("$.contentType"),
            content: JsonPath.jsonToString(JsonPath.objectAt("$.content")),
            ...(includeClassification && {
              classification: JsonPath.stringAt(
                "$.classificationResult.documentClassification"
              ),
            }),
          }),
        },
      ],
    });
  }

  private invokeFunction(
    id: string,
    invokeType: "classification" | "processing",
    modelId: string,
    prompt: string
  ): lambda.Function {
    const entry = path.join(__dirname, "resources/model-invoke-lambda");
    const baseName = `idp-${invokeType}`;
    const functionName = this.uniqueFunctionName(baseName);
    const functionNodeId = `${id}Function`;
    const fn = new lambda.Function(this, functionNodeId, {
      functionName,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(entry, {
        exclude: ["__pycache__", "**/__pycache__", "*.pyc"],
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
                execSync(
                  `pip3 install -r "${entry}/requirements.txt" -t "${outputDir}"` +
                    " --no-cache-dir --quiet --only-binary=:all:" +
                    " --platform manylinux2014_x86_64 --python-version 3.12 --implementation cp --abi cp312" +
                    ` && cp -r "${entry}/." "${outputDir}/"` +
                    ` && find "${outputDir}" -name __pycache__ -type d -exec rm -rf {} +`,
                  { stdio: "pipe", shell: "/bin/bash" }
                );
                return true;
              } catch {
                return false;
              }
            },
          },
        },
      }),
      timeout: this.props.stepTimeouts ?? cdk.Duration.minutes(5),
      memorySize: 512,
      description: `Document ${invokeType} via Bedrock Converse (model-agnostic)`,
      logGroup: this.lambdaLogGroup(functionNodeId, functionName),
      tracing: this.props.enableObservability
        ? lambda.Tracing.ACTIVE
        : lambda.Tracing.DISABLED,
      environmentEncryption: this.props.encryptionKey,
      environment: {
        MODEL_ID: modelId,
        PROMPT: prompt,
        INVOKE_TYPE: invokeType,
        ...(this.props.maxDocumentBytes !== undefined && {
          MAX_MODEL_DOC_BYTES: String(this.props.maxDocumentBytes),
        }),
        ...(this.props.largeDocFallbackType && {
          LARGE_DOC_FALLBACK_TYPE: this.props.largeDocFallbackType,
        }),
        ...(this.props.useStructuredOutput && { MODEL_INVOKE_STRUCTURED: "true" }),
        ...(this.props.enableObservability
          ? {
              POWERTOOLS_METRICS_NAMESPACE: this.metricNamespace,
              POWERTOOLS_SERVICE_NAME: this.metricServiceName,
              LOG_LEVEL: "INFO",
            }
          : {
              POWERTOOLS_METRICS_DISABLED: "true",
              POWERTOOLS_TRACE_DISABLED: "true",
              LOG_LEVEL: "INFO",
            }),
      },
    });
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          `arn:aws:bedrock:*:${cdk.Stack.of(this).account}:inference-profile/*`,
        ],
      })
    );
    this.documentBucket.grantRead(fn);
    return fn;
  }

  private uniqueFunctionName(baseName: string): string {
    return `${baseName}-${cdk.Names.uniqueResourceName(this, {
      maxLength: 64 - (baseName.length + 1),
    }).toLowerCase()}`;
  }

  private lambdaLogGroup(nodeId: string, functionName: string): logs.LogGroup | undefined {
    if (!this.props.enableObservability) return undefined;
    return new logs.LogGroup(this, `${nodeId}-LogGroup`, {
      logGroupName: `/aws/lambda/${functionName}-secured`,
      retention: logs.RetentionDays.TWO_YEARS,
      encryptionKey: this.props.encryptionKey,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
  }

  private classificationStep(): LambdaInvoke {
    if (!this.classificationFn) {
      this.classificationFn = this.invokeFunction(
        "MultiModelClassification",
        "classification",
        this.props.classificationModelId ??
          this.props.classificationBedrockModel?.customModelId ??
          "",
        this.props.classificationPrompt ?? ""
      );
    }
    return new LambdaInvoke(
      this,
      `MultiModelClassificationStep-${this.classificationSteps++}`,
      {
        lambdaFunction: this.classificationFn,
        resultPath: "$.classificationResult",
        resultSelector: {
          "documentClassification.$": "$.Payload.documentClassification",
          "modelId.$": "$.Payload.modelId",
          "usage.$": "$.Payload.usage",
        },
      }
    );
  }

  private processingStep(): LambdaInvoke {
    if (!this.processingFn) {
      this.processingFn = this.invokeFunction(
        "MultiModelProcessing",
        "processing",
        this.props.processingModelId ??
          this.props.processingBedrockModel?.customModelId ??
          "",
        this.props.processingPrompt ?? ""
      );
    }
    return new LambdaInvoke(
      this,
      `MultiModelProcessingStep-${this.processingSteps++}`,
      {
        lambdaFunction: this.processingFn,
        resultPath: "$.processingResult",
        resultSelector: {
          "documentClassification.$": "$.Payload.documentClassification",
          "result.$": "$.Payload.result",
          "modelId.$": "$.Payload.modelId",
          "usage.$": "$.Payload.usage",
        },
      }
    );
  }

  private addS3Notification(): void {
    const destination: s3.IBucketNotificationDestination = {
      bind: (_scope, bucket) => {
        this.documentQueue.addToResourcePolicy(
          new iam.PolicyStatement({
            principals: [new iam.ServicePrincipal("s3.amazonaws.com")],
            actions: ["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"],
            resources: [this.documentQueue.queueArn],
            conditions: {
              ArnLike: { "aws:SourceArn": this.documentBucket.bucketArn },
            },
          })
        );
        if (this.documentQueue.encryptionMasterKey) {
          this.documentQueue.encryptionMasterKey.addToResourcePolicy(
            new iam.PolicyStatement({
              principals: [new iam.ServicePrincipal("s3.amazonaws.com")],
              actions: ["kms:GenerateDataKey*", "kms:Decrypt"],
              resources: ["*"],
            }),
            true
          );
        }
        return {
          arn: this.documentQueue.queueArn,
          type: s3.BucketNotificationDestinationType.QUEUE,
          dependencies: [this.documentQueue],
        };
      },
    };
    this.documentBucket.addEventNotification(s3.EventType.OBJECT_CREATED, destination, {
      prefix: "raw/",
    });
  }
}
