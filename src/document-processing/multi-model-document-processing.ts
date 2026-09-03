import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { JsonPath } from "aws-cdk-lib/aws-stepfunctions";
import {
  DynamoAttributeValue,
  LambdaInvoke,
} from "aws-cdk-lib/aws-stepfunctions-tasks";
import {
  BedrockDocumentProcessing,
  BedrockDocumentProcessingProps,
  LambdaIamUtils,
} from "@cdklabs/cdk-appmod-catalog-blueprints";
import { Construct } from "constructs";

/**
 * Per-step model configuration.
 *
 * The blueprint takes a single model for classification and another for
 * extraction, both assumed to be Anthropic. These props let each step run on a
 * different Bedrock model so an expensive step can be moved to a stronger
 * model for accuracy, or a cheap one to a smaller model for cost, without a
 * code change.
 */
export interface MultiModelDocumentProcessingProps
  extends BedrockDocumentProcessingProps {
  /** Model id for the classification step. Defaults to `classificationBedrockModel`. */
  readonly classificationModelId?: string;

  /** Model id for the extraction step. Defaults to `processingBedrockModel`. */
  readonly processingModelId?: string;

  /**
   * Byte ceiling above which a document skips the model and degrades to
   * `largeDocFallbackType`. Depends on the configured model's context window —
   * retune when switching to a model with a different one.
   * @default 3500000 (suits a ~200K-token window)
   */
  readonly maxDocumentBytes?: number;

  /** Type returned when a document exceeds `maxDocumentBytes`. */
  readonly largeDocFallbackType?: string;

  /**
   * Constrain the model with a forced tool call instead of asking it to write
   * JSON as free text. More reliable across model families, but changes how
   * the model is invoked — leave off until the plain-text path is verified on
   * the target model.
   * @default false
   */
  readonly useStructuredOutput?: boolean;
}

/**
 * Per-instance state for the step overrides.
 *
 * This cannot be class fields. `BedrockDocumentProcessing`'s constructor builds
 * the state machine on its last line, which calls `classificationStep()` and
 * `processingStep()` — i.e. our overrides run *during* `super()`, before a
 * subclass field initializer has executed. With `target: es2022`, TypeScript
 * emits `[[Define]]` semantics for declared fields, so any field we declared
 * would be reset to its initializer immediately after `super()` returns,
 * silently discarding whatever the overrides stored. A WeakMap keyed by the
 * instance sidesteps the ordering entirely.
 */
interface StepState {
  classificationFn?: lambda.Function;
  processingFn?: lambda.Function;
  classificationSteps: number;
  processingSteps: number;
}
const STATE = new WeakMap<MultiModelDocumentProcessing, StepState>();

/**
 * Document processing pipeline whose model-invoking steps work with any
 * Bedrock-hosted model.
 *
 * The blueprint's own invoke Lambda calls `invoke_model` with an
 * Anthropic-native body, so only Anthropic models work with it. We used to fix
 * that by overwriting the file inside `node_modules` at build time, which a
 * dependency bump would silently revert. This subclass instead uses the
 * blueprint's documented extension points (`classificationStep` /
 * `processingStep`) to point those steps at our own Lambda
 * (`resources/model-invoke-lambda`), which speaks the model-agnostic Converse
 * API.
 *
 * Everything else — the state machine, retries, DynamoDB tracking, the
 * ingestion adapter — still comes from the blueprint.
 */
export class MultiModelDocumentProcessing extends BedrockDocumentProcessing {
  constructor(
    scope: Construct,
    id: string,
    props: MultiModelDocumentProcessingProps
  ) {
    super(scope, id, props);
  }

  /** Config, read from props rather than fields — see {@link StepState}. */
  private get modelProps(): MultiModelDocumentProcessingProps {
    return this.bedrockDocumentProcessingProps as MultiModelDocumentProcessingProps;
  }

  private get state(): StepState {
    let s = STATE.get(this);
    if (!s) {
      s = { classificationSteps: 0, processingSteps: 0 };
      STATE.set(this, s);
    }
    return s;
  }

  /**
   * Preserve the stable public job lifecycle when the blueprint replaces the
   * lightweight AWAITING_UPLOAD row in InitMetadataEntry.
   */
  protected preprocessingMetadata(): Record<string, DynamoAttributeValue> {
    return {
      ...super.preprocessingMetadata(),
      ProcessingStatus: DynamoAttributeValue.fromString("PROCESSING"),
      ProcessingStatusUpdatedAt: DynamoAttributeValue.fromString(
        JsonPath.stringAt("$.eventTime")
      ),
      JobOwner: DynamoAttributeValue.fromString(JsonPath.stringAt("$.jobOwner")),
      RequestedDocumentType: DynamoAttributeValue.fromString(
        JsonPath.stringAt("$.requestedDocumentType")
      ),
      RegisteredAt: DynamoAttributeValue.fromString(
        JsonPath.stringAt("$.registeredAt")
      ),
      UploadExpiresAt: DynamoAttributeValue.fromString(
        JsonPath.stringAt("$.uploadExpiresAt")
      ),
    };
  }

  /**
   * Builds (once) the Lambda backing a step.
   *
   * Bundling mirrors the enrichment Lambda in the stack: a local `pip install`
   * when available, falling back to the official Python bundling container.
   * `AssetHashType.OUTPUT` hashes the bundled result, so a change to how
   * dependencies resolve always produces a new asset — hashing only the source
   * directory has silently reused a stale, broken build here before.
   */
  private invokeFunction(
    stepId: string,
    invokeType: "classification" | "processing",
    modelId: string,
    prompt: string
  ): lambda.Function {
    const entry = path.join(__dirname, "resources/model-invoke-lambda");
    const p = this.modelProps;
    const { region, account } = cdk.Stack.of(this);

    // An explicit function name is required, not cosmetic: when observability
    // is on, the blueprint injects a log group named
    // `/aws/lambda/${functionName}-secured`. In prod it also grants the shared
    // encryption key against that exact ARN. Leaving the name undefined yields
    // a literal `/aws/lambda/undefined-secured` log group.
    const logs = LambdaIamUtils.createLogsPermissions({
      scope: this,
      functionName: `idp-${invokeType}`,
      region,
      account,
      enableObservability: p.enableObservability,
    });

    const fn = new lambda.Function(this, `${stepId}Function`, {
      functionName: logs.uniqueFunctionName,
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
                    ` --no-cache-dir --quiet --only-binary=:all:` +
                    ` --platform manylinux2014_x86_64 --python-version 3.12 --implementation cp --abi cp312` +
                    ` && cp -r "${entry}/." "${outputDir}/"` +
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
      timeout: p.stepTimeouts ?? cdk.Duration.minutes(5),
      memorySize: 512,
      description: `Document ${invokeType} via Bedrock Converse (model-agnostic)`,
      // Reuses the blueprint's own role builder so the step keeps exactly the
      // Bedrock permissions the blueprint would have granted it.
      role: this.generateLambdaRoleForBedrock(
        `${stepId}LambdaRole`,
        invokeType === "classification"
          ? p.classificationBedrockModel
          : p.processingBedrockModel
      ),
      environment: {
        MODEL_ID: modelId,
        PROMPT: prompt,
        INVOKE_TYPE: invokeType,
        ...(p.maxDocumentBytes !== undefined && {
          MAX_MODEL_DOC_BYTES: String(p.maxDocumentBytes),
        }),
        ...(p.largeDocFallbackType && {
          LARGE_DOC_FALLBACK_TYPE: p.largeDocFallbackType,
        }),
        ...(p.useStructuredOutput && { MODEL_INVOKE_STRUCTURED: "true" }),
        // Powertools' own env-var contract, set directly rather than through
        // the blueprint's internal helper (which its entry point doesn't export).
        ...(p.enableObservability
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
      environmentEncryption: this.encryptionKey,
    });

    for (const statement of logs.policyStatements) {
      fn.role?.addToPrincipalPolicy(statement);
    }
    this.encryptionKey?.grantEncryptDecrypt(fn);
    return fn;
  }

  /**
   * Classification step, backed by our Converse Lambda.
   *
   * `resultPath` and `resultSelector` reproduce the blueprint's contract
   * exactly — downstream states read `$.classificationResult`.
   */
  protected classificationStep(): LambdaInvoke {
    const s = this.state;
    if (!s.classificationFn) {
      const p = this.modelProps;
      s.classificationFn = this.invokeFunction(
        "MultiModelClassification",
        "classification",
        p.classificationModelId ??
          p.classificationBedrockModel?.customModelId ??
          "",
        p.classificationPrompt ?? ""
      );
    }

    // A new task per call: the blueprint chains this step more than once
    // (the chunked flow builds its own copy), and construct IDs must be unique.
    const stepId = `MultiModelClassificationStep-${s.classificationSteps++}`;
    return new LambdaInvoke(this, stepId, {
      lambdaFunction: s.classificationFn,
      resultPath: "$.classificationResult",
      resultSelector: {
        "documentClassification.$": "$.Payload.documentClassification",
        // Carried through so the enrichment Lambda can record which model ran
        // in each phase. Null when a bypass answered without calling a model.
        "modelId.$": "$.Payload.modelId",
      },
    });
  }

  /** Extraction step. Mirrors the blueprint's `$.processingResult` contract. */
  protected processingStep(): LambdaInvoke {
    const s = this.state;
    if (!s.processingFn) {
      const p = this.modelProps;
      s.processingFn = this.invokeFunction(
        "MultiModelProcessing",
        "processing",
        p.processingModelId ?? p.processingBedrockModel?.customModelId ?? "",
        p.processingPrompt ?? ""
      );
    }

    const stepId = `MultiModelProcessingStep-${s.processingSteps++}`;
    return new LambdaInvoke(this, stepId, {
      lambdaFunction: s.processingFn,
      resultPath: "$.processingResult",
      resultSelector: {
        "documentClassification.$": "$.Payload.documentClassification",
        "result.$": "$.Payload.result",
        "modelId.$": "$.Payload.modelId",
      },
    });
  }
}
