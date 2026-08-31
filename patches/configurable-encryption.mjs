import { readFileSync, writeFileSync } from "fs";
import { join } from "path";

const SUPPORTED_VERSION = "1.17.0";

function replaceRequired(source, before, after, label) {
  if (source.includes(before)) return source.replace(before, after);
  if (source.includes(after)) return source;
  throw new Error(`Encryption patch could not find ${label}`);
}

function patchFile(packageRoot, relativePath, transforms) {
  const file = join(packageRoot, relativePath);
  let source = readFileSync(file, "utf8");
  for (const [before, after, label] of transforms) {
    source = replaceRequired(source, before, after, `${relativePath}: ${label}`);
  }
  writeFileSync(file, source);
}

/**
 * The upstream construct always creates customer-managed KMS keys. This
 * project needs a cheaper dev mode that keeps AWS-owned/default encryption,
 * and a prod mode where one supplied CMK is reused by every component.
 *
 * Keep this patch pinned and fail closed: if the package changes, a build must
 * stop instead of silently synthesizing extra billable keys.
 */
export function patchBlueprintEncryption(packageRoot) {
  const packageJson = JSON.parse(
    readFileSync(join(packageRoot, "package.json"), "utf8")
  );
  if (packageJson.version !== SUPPORTED_VERSION) {
    throw new Error(
      `Configurable-encryption patch supports blueprint ${SUPPORTED_VERSION}, found ${packageJson.version}`
    );
  }

  patchFile(packageRoot, "lib/document-processing/base-document-processing.d.ts", [
    [
      "    readonly encryptionKey?: Key;\n}",
      "    readonly encryptionKey?: Key;\n    /**\n     * Use a customer-managed KMS key. When false, every service uses its\n     * AWS-owned/default encryption and no KMS key is created.\n     * @default true\n     */\n    readonly useCustomerManagedEncryption?: boolean;\n}",
      "BaseDocumentProcessingProps.useCustomerManagedEncryption",
    ],
    [
      "    readonly encryptionKey: Key;",
      "    readonly encryptionKey?: Key;",
      "optional BaseDocumentProcessing.encryptionKey",
    ],
  ]);

  patchFile(packageRoot, "lib/document-processing/base-document-processing.js", [
    [
      `        this.props = props;
        this.ingressAdapter = props.ingressAdapter || new adapter_1.QueuedS3Adapter();`,
      `        const useCustomerManagedEncryption = props.useCustomerManagedEncryption ?? true;
        if (!useCustomerManagedEncryption && props.encryptionKey) {
            throw new Error('encryptionKey cannot be supplied when useCustomerManagedEncryption is false');
        }
        this.encryptionKey = useCustomerManagedEncryption ? props.encryptionKey || new aws_kms_1.Key(this, 'IDPEncryptionKey', {
            enableKeyRotation: true,
            removalPolicy: props.removalPolicy || aws_cdk_lib_1.RemovalPolicy.DESTROY,
        }) : undefined;
        this.props = { ...props, encryptionKey: this.encryptionKey };
        this.ingressAdapter = props.ingressAdapter || new adapter_1.QueuedS3Adapter();`,
      "environment-aware base key creation",
    ],
    [
      `        this.ingressAdapter.init(this, props);
        this.encryptionKey = props.encryptionKey || new aws_kms_1.Key(this, 'IDPEncryptionKey', {
            enableKeyRotation: true,
            removalPolicy: props.removalPolicy || aws_cdk_lib_1.RemovalPolicy.DESTROY,
        });
        this.logGroupDataProtection = utilities_1.LogGroupDataProtectionUtils.handleDefault(this, props.logGroupDataProtection, props.removalPolicy);`,
      `        this.ingressAdapter.init(this, this.props);
        this.logGroupDataProtection = useCustomerManagedEncryption
            ? utilities_1.LogGroupDataProtectionUtils.handleDefault(this, props.logGroupDataProtection, props.removalPolicy)
            : (props.logGroupDataProtection || {});`,
      "reuse base key in adapter and skip dev log key",
    ],
    [
      `            encryption: aws_dynamodb_1.TableEncryption.CUSTOMER_MANAGED,
            encryptionKey: this.encryptionKey,`,
      `            encryption: useCustomerManagedEncryption
                ? aws_dynamodb_1.TableEncryption.CUSTOMER_MANAGED
                : aws_dynamodb_1.TableEncryption.DEFAULT,
            encryptionKey: this.encryptionKey,`,
      "default DynamoDB encryption in dev",
    ],
    [
      `        this.encryptionKey.grantEncryptDecrypt(role);`,
      `        this.encryptionKey?.grantEncryptDecrypt(role);`,
      "optional state-machine KMS grant",
    ],
    [
      `            encryptionConfiguration: new aws_stepfunctions_1.CustomerManagedEncryptionConfiguration(this.encryptionKey),`,
      `            encryptionConfiguration: this.encryptionKey
                ? new aws_stepfunctions_1.CustomerManagedEncryptionConfiguration(this.encryptionKey)
                : new aws_stepfunctions_1.AwsOwnedEncryptionConfiguration(),`,
      "AWS-owned Step Functions encryption in dev",
    ],
  ]);

  patchFile(packageRoot, "lib/document-processing/adapter/queued-s3-adapter.js", [
    [
      `        const encryptionKey = props.encryptionKey || new aws_kms_1.Key(scope, 'QueuedS3AdapterEncryptionKey', {
            enableKeyRotation: true,
            removalPolicy: props.removalPolicy || aws_cdk_lib_1.RemovalPolicy.DESTROY,
        });`,
      `        const useCustomerManagedEncryption = props.useCustomerManagedEncryption ?? true;
        const encryptionKey = useCustomerManagedEncryption
            ? props.encryptionKey || new aws_kms_1.Key(scope, 'QueuedS3AdapterEncryptionKey', {
                enableKeyRotation: true,
                removalPolicy: props.removalPolicy || aws_cdk_lib_1.RemovalPolicy.DESTROY,
            })
            : undefined;`,
      "environment-aware adapter key",
    ],
    [
      `            encryption: aws_s3_1.BucketEncryption.KMS, // Uses AWS-managed KMS key for encryption at rest
            enforceSSL: true,
            bucketKeyEnabled: true, // Reduces KMS costs by using S3 Bucket Keys`,
      `            encryption: useCustomerManagedEncryption
                ? aws_s3_1.BucketEncryption.KMS
                : aws_s3_1.BucketEncryption.S3_MANAGED,
            encryptionKey,
            enforceSSL: true,
            bucketKeyEnabled: useCustomerManagedEncryption,`,
      "shared prod key or SSE-S3 bucket",
    ],
    [
      `            encryption: aws_sqs_1.QueueEncryption.KMS,
            encryptionMasterKey: encryptionKey,`,
      `            encryption: useCustomerManagedEncryption
                ? aws_sqs_1.QueueEncryption.KMS
                : aws_sqs_1.QueueEncryption.SQS_MANAGED,
            encryptionMasterKey: encryptionKey,`,
      "SQS-managed DLQ encryption in dev",
    ],
    [
      `            encryption: aws_sqs_1.QueueEncryption.KMS,
            encryptionMasterKey: encryptionKey,`,
      `            encryption: useCustomerManagedEncryption
                ? aws_sqs_1.QueueEncryption.KMS
                : aws_sqs_1.QueueEncryption.SQS_MANAGED,
            encryptionMasterKey: encryptionKey,`,
      "SQS-managed queue encryption in dev",
    ],
    [
      `        encryptionKey.grantEncryptDecrypt(sqsConsumerRole);`,
      `        encryptionKey?.grantEncryptDecrypt(sqsConsumerRole);`,
      "optional SQS consumer KMS grant",
    ],
    [
      `        bucket.addEventNotification(aws_s3_1.EventType.OBJECT_CREATED, new aws_s3_notifications_1.SqsDestination(queue), {
            prefix: this.prefixes.raw,
        });`,
      `        // SqsDestination.bind() calls queue.grantSendMessages() with a
        // SourceArn condition containing this bucket. For an encrypted queue,
        // that condition is copied into the KMS key policy. When the bucket and
        // queue share the same key, CloudFormation then sees Key -> Bucket while
        // the bucket's default encryption already requires Bucket -> Key.
        //
        // Bind the equivalent destination explicitly. The queue policy remains
        // scoped to this bucket, and the KMS service grant is the same
        // unconditional grant SqsDestination adds immediately afterwards, but
        // the redundant bucket reference never enters the key policy.
        const queueDestination = {
            bind: (_scope, sourceBucket) => {
                queue.addToResourcePolicy(new aws_iam_1.PolicyStatement({
                    effect: aws_iam_1.Effect.ALLOW,
                    principals: [new aws_iam_1.ServicePrincipal('s3.amazonaws.com')],
                    actions: ['sqs:SendMessage', 'sqs:GetQueueAttributes', 'sqs:GetQueueUrl'],
                    resources: [queue.queueArn],
                    conditions: { ArnLike: { 'aws:SourceArn': sourceBucket.bucketArn } },
                }));
                if (queue.encryptionMasterKey) {
                    queue.encryptionMasterKey.addToResourcePolicy(new aws_iam_1.PolicyStatement({
                        principals: [new aws_iam_1.ServicePrincipal('s3.amazonaws.com')],
                        actions: ['kms:GenerateDataKey*', 'kms:Decrypt'],
                        resources: ['*'],
                    }), true);
                }
                return {
                    arn: queue.queueArn,
                    type: aws_s3_1.BucketNotificationDestinationType.QUEUE,
                    dependencies: [queue],
                };
            },
        };
        bucket.addEventNotification(aws_s3_1.EventType.OBJECT_CREATED, queueDestination, {
            prefix: this.prefixes.raw,
        });`,
      "cycle-free encrypted S3 to SQS notification",
    ],
  ]);

  patchFile(packageRoot, "lib/document-processing/bedrock-document-processing.js", [
    [
      "this.encryptionKey.grantEncryptDecrypt(role);",
      "this.encryptionKey?.grantEncryptDecrypt(role);",
      "optional Bedrock Lambda KMS grants",
    ],
  ]);

  // The same grant appears in optional chunking/aggregation paths. Replace all
  // remaining occurrences after the required first replacement above.
  const bedrockFile = join(
    packageRoot,
    "lib/document-processing/bedrock-document-processing.js"
  );
  const bedrockSource = readFileSync(bedrockFile, "utf8").replaceAll(
    "this.encryptionKey.grantEncryptDecrypt(role);",
    "this.encryptionKey?.grantEncryptDecrypt(role);"
  );
  writeFileSync(bedrockFile, bedrockSource);

  patchFile(packageRoot, "lib/framework/foundation/eventbridge-broker.d.ts", [
    [
      "    readonly removalPolicy?: RemovalPolicy;\n}",
      "    readonly removalPolicy?: RemovalPolicy;\n    /** @default true */\n    readonly useCustomerManagedEncryption?: boolean;\n}",
      "EventbridgeBrokerProps.useCustomerManagedEncryption",
    ],
    [
      "    readonly kmsKey: Key;",
      "    readonly kmsKey?: Key;",
      "optional EventbridgeBroker.kmsKey",
    ],
  ]);

  patchFile(packageRoot, "lib/framework/foundation/eventbridge-broker.js", [
    [
      `        this.kmsKey = props.kmsKey || new aws_kms_1.Key(this, 'EventBrokerKMSKey', {
            enableKeyRotation: true,
            removalPolicy,
        });`,
      `        const useCustomerManagedEncryption = props.useCustomerManagedEncryption ?? true;
        if (!useCustomerManagedEncryption && props.kmsKey) {
            throw new Error('kmsKey cannot be supplied when useCustomerManagedEncryption is false');
        }
        this.kmsKey = useCustomerManagedEncryption ? props.kmsKey || new aws_kms_1.Key(this, 'EventBrokerKMSKey', {
            enableKeyRotation: true,
            removalPolicy,
        }) : undefined;`,
      "environment-aware EventBridge key",
    ],
  ]);
}
