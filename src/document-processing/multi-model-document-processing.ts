import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as events from "aws-cdk-lib/aws-events";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as lambdaDestinations from "aws-cdk-lib/aws-lambda-destinations";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3Notifications from "aws-cdk-lib/aws-s3-notifications";
import { Construct } from "constructs";

export interface MultiModelDocumentProcessingProps {
  readonly enrichmentLambdaFunction: lambda.Function;
  readonly eventBus: events.IEventBus;
  readonly catalogTable?: dynamodb.ITable;
  readonly eventSource?: string;
  readonly enableObservability?: boolean;
  readonly metricNamespace?: string;
  readonly metricServiceName?: string;
  readonly useCustomerManagedEncryption?: boolean;
  readonly encryptionKey?: kms.IKey;
  readonly removalPolicy?: cdk.RemovalPolicy;
  readonly enableIngressNotifications?: boolean;
}

/**
 * Project-owned direct document ingress.
 *
 * The historical construct and stateful child ids are deliberately preserved:
 * CloudFormation keeps the existing bucket/table in place while removing the
 * SQS consumer, model-invoke Lambdas and Standard Step Functions workflow.
 * Only source/ and the legacy raw/ prefix invoke Enrichment; results/ and
 * references/ can therefore never recursively trigger document processing.
 */
export class MultiModelDocumentProcessing extends Construct {
  readonly documentProcessingTable: dynamodb.Table;
  readonly documentBucket: s3.Bucket;
  readonly metricNamespace: string;
  readonly metricServiceName: string;
  readonly asyncFailureFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: MultiModelDocumentProcessingProps) {
    super(scope, id);
    this.metricNamespace = props.metricNamespace ?? "bedrock-document-processing";
    this.metricServiceName = props.metricServiceName ?? "extraction-workflow";

    const useKms = props.useCustomerManagedEncryption ?? Boolean(props.encryptionKey);
    if (useKms && !props.encryptionKey) {
      throw new Error("Customer-managed encryption requires encryptionKey");
    }
    if (!useKms && props.encryptionKey) {
      throw new Error(
        "encryptionKey cannot be supplied when customer-managed encryption is disabled"
      );
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

    const failureName = this.uniqueFunctionName("AsyncFailure");
    this.asyncFailureFunction = new lambda.Function(this, "AsyncFailureFinalizer", {
      functionName: failureName,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "resources/async-failure-lambda"),
        { exclude: ["__pycache__", "*.pyc"] }
      ),
      timeout: cdk.Duration.seconds(30),
      memorySize: 128,
      description:
        "Finalizes document jobs whose direct asynchronous enrichment exhausted retries",
      logGroup: this.lambdaLogGroup("AsyncFailureFinalizer", failureName, props),
      tracing: props.enableObservability ? lambda.Tracing.ACTIVE : lambda.Tracing.DISABLED,
      environmentEncryption: props.encryptionKey,
      environment: {
        DOCUMENT_TABLE: this.documentProcessingTable.tableName,
        EVENT_BUS_NAME: props.eventBus.eventBusName,
        EVENT_SOURCE: props.eventSource ?? "document-validation-pipeline",
      },
    });
    this.documentProcessingTable.grant(
      this.asyncFailureFunction,
      "dynamodb:UpdateItem"
    );
    props.eventBus.grantPutEventsTo(this.asyncFailureFunction);

    props.enrichmentLambdaFunction.addEnvironment(
      "EVENT_BUS_NAME",
      props.eventBus.eventBusName
    );
    props.enrichmentLambdaFunction.addEnvironment(
      "EVENT_SOURCE",
      props.eventSource ?? "document-validation-pipeline"
    );
    props.enrichmentLambdaFunction.addEnvironment("DOCUMENT_SOURCE_PREFIX", "source/");
    props.eventBus.grantPutEventsTo(props.enrichmentLambdaFunction);

    new lambda.EventInvokeConfig(this, "EnrichmentAsyncInvokeConfig", {
      function: props.enrichmentLambdaFunction,
      maxEventAge: cdk.Duration.hours(6),
      retryAttempts: 2,
      onFailure: new lambdaDestinations.LambdaDestination(
        this.asyncFailureFunction
      ),
    });

    if (props.enableIngressNotifications !== false) {
      const destination = new s3Notifications.LambdaDestination(
        props.enrichmentLambdaFunction
      );
      this.documentBucket.addEventNotification(
        s3.EventType.OBJECT_CREATED,
        destination,
        { prefix: "source/" }
      );
      // Compatibility for already-issued presigned URLs and documented direct
      // uploads. New API uploads use source/; raw/ remains an accepted ingress
      // but is no longer moved after processing.
      this.documentBucket.addEventNotification(
        s3.EventType.OBJECT_CREATED,
        destination,
        { prefix: "raw/" }
      );
    }
  }

  private uniqueFunctionName(baseName: string): string {
    return `${baseName}-${cdk.Names.uniqueResourceName(this, {
      maxLength: 64 - (baseName.length + 1),
    }).toLowerCase()}`;
  }

  private lambdaLogGroup(
    nodeId: string,
    functionName: string,
    props: MultiModelDocumentProcessingProps
  ): logs.LogGroup | undefined {
    if (!props.enableObservability) return undefined;
    return new logs.LogGroup(this, `${nodeId}-LogGroup`, {
      logGroupName: `/aws/lambda/${functionName}-secured`,
      retention: logs.RetentionDays.TWO_YEARS,
      encryptionKey: props.encryptionKey,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
  }
}
