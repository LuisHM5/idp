#!/usr/bin/env node
/** Deployment gate for the direct S3 -> Enrichment migration. */
import fs from "node:fs";
import path from "node:path";

const envArg = process.argv.indexOf("--env");
const environment = envArg >= 0 ? process.argv[envArg + 1] : "dev";
const stackName = environment === "prod" ? "DocumentValidationStack" : "DocumentValidationStack-Dev";
const templatePath = path.resolve("cdk.out", `${stackName}.template.json`);

if (!fs.existsSync(templatePath)) {
  throw new Error(`No existe ${templatePath}; ejecuta synth antes de esta verificación`);
}
const template = JSON.parse(fs.readFileSync(templatePath, "utf8"));
const resources = template.Resources || {};

const expected = {
  IDPBrokerEventBus3145175C: "AWS::Events::EventBus",
  BedrockDocumentProcessingDocumentProcessingBucket32BD72F9: "AWS::S3::Bucket",
  BedrockDocumentProcessingDocumentProcessingTableD87A7B20: "AWS::DynamoDB::Table",
};
for (const [logicalId, type] of Object.entries(expected)) {
  if (resources[logicalId]?.Type !== type) {
    throw new Error(`Migración insegura: falta ${logicalId} (${type})`);
  }
}

for (const forbidden of [
  "AWS::SQS::Queue",
  "AWS::Lambda::EventSourceMapping",
  "AWS::StepFunctions::StateMachine",
]) {
  const found = Object.entries(resources).filter(([, resource]) => resource.Type === forbidden);
  if (found.length) {
    throw new Error(`El ingress directo no debe sintetizar ${forbidden}: ${found.map(([id]) => id).join(", ")}`);
  }
}

const bucketNotifications = Object.values(resources).find(
  (resource) => resource.Type === "Custom::S3BucketNotifications"
);
const lambdaConfigurations =
  bucketNotifications?.Properties?.NotificationConfiguration?.LambdaFunctionConfigurations || [];
const prefixes = lambdaConfigurations.flatMap((entry) =>
  (entry.Filter?.Key?.FilterRules || [])
    .filter((rule) => rule.Name === "prefix")
    .map((rule) => rule.Value)
);
for (const required of ["source/", "raw/"]) {
  if (!prefixes.includes(required)) throw new Error(`Falta trigger S3 directo para ${required}`);
}
for (const forbiddenPrefix of ["results/", "processed/", "failed/", "references/"]) {
  if (prefixes.includes(forbiddenPrefix)) {
    throw new Error(`El prefijo ${forbiddenPrefix} causaría un bucle de procesamiento`);
  }
}

const asyncConfig = Object.values(resources).find(
  (resource) => resource.Type === "AWS::Lambda::EventInvokeConfig"
);
if (
  !asyncConfig ||
  asyncConfig.Properties?.MaximumRetryAttempts !== 2 ||
  !asyncConfig.Properties?.DestinationConfig?.OnFailure
) {
  throw new Error("El invoke asíncrono debe conservar 2 reintentos y destino de falla");
}

const serialized = JSON.stringify(template);
for (const removed of ["InitMetadataEntry", "StartExecution", "CopyToProcessed", "DeleteFromRawSuccess"]) {
  if (serialized.includes(removed)) throw new Error(`Persistió el componente eliminado ${removed}`);
}

console.log(
  `Infra migration contract OK (${stackName}): bucket/tabla conservados, ` +
    "S3 directo limitado a source/raw, sin SQS ni Step Functions y con recuperación asíncrona"
);
