#!/usr/bin/env node
/**
 * Synthesizes dev and prod, then enforces the project's encryption contract:
 * zero customer-managed KMS keys in dev and exactly one shared key in prod.
 */
import { readFileSync } from "fs";
import { spawnSync } from "child_process";
import { resolve } from "path";

const ROOT = resolve(import.meta.dirname, "..");
const NPX = process.platform === "win32" ? "npx.cmd" : "npx";

function fail(message) {
  throw new Error(`Encryption template check failed: ${message}`);
}

function synth(stack, environment) {
  const result = spawnSync(
    NPX,
    ["cdk", "synth", stack, "-c", `env=${environment}`, "--quiet"],
    { cwd: ROOT, stdio: "inherit" }
  );
  if (result.status !== 0) process.exit(result.status ?? 1);
}

function template(name) {
  return JSON.parse(readFileSync(resolve(ROOT, "cdk.out", name), "utf8"));
}

function resourcesOfType(tpl, type) {
  return Object.entries(tpl.Resources).filter(([, resource]) => resource.Type === type);
}

function collectResourceReferences(value, logicalIds, references) {
  if (Array.isArray(value)) {
    value.forEach((item) => collectResourceReferences(item, logicalIds, references));
    return;
  }
  if (!value || typeof value !== "object") return;

  if (typeof value.Ref === "string" && logicalIds.has(value.Ref)) {
    references.add(value.Ref);
  }

  const getAtt = value["Fn::GetAtt"];
  const getAttResource = Array.isArray(getAtt)
    ? getAtt[0]
    : typeof getAtt === "string"
      ? getAtt.split(".", 1)[0]
      : undefined;
  if (getAttResource && logicalIds.has(getAttResource)) {
    references.add(getAttResource);
  }

  const sub = value["Fn::Sub"];
  if (typeof sub === "string") {
    for (const match of sub.matchAll(/\$\{([A-Za-z0-9]+)(?:\.[^}]*)?\}/g)) {
      if (logicalIds.has(match[1])) references.add(match[1]);
    }
  }

  Object.values(value).forEach((child) =>
    collectResourceReferences(child, logicalIds, references)
  );
}

function assertNoCircularResourceDependencies(tpl, environment) {
  const logicalIds = new Set(Object.keys(tpl.Resources));
  const dependencies = new Map();

  for (const [logicalId, resource] of Object.entries(tpl.Resources)) {
    const references = new Set();
    collectResourceReferences(resource.Properties, logicalIds, references);
    const explicit = Array.isArray(resource.DependsOn)
      ? resource.DependsOn
      : resource.DependsOn
        ? [resource.DependsOn]
        : [];
    explicit.forEach((dependency) => {
      if (logicalIds.has(dependency)) references.add(dependency);
    });
    dependencies.set(logicalId, references);
  }

  const visited = new Set();
  const visiting = new Set();
  const path = [];

  function visit(logicalId) {
    if (visited.has(logicalId)) return;
    if (visiting.has(logicalId)) {
      const cycleStart = path.indexOf(logicalId);
      const cycle = [...path.slice(cycleStart), logicalId].join(" -> ");
      fail(`${environment} contains a circular resource dependency: ${cycle}`);
    }

    visiting.add(logicalId);
    path.push(logicalId);
    for (const dependency of dependencies.get(logicalId) || []) {
      visit(dependency);
    }
    path.pop();
    visiting.delete(logicalId);
    visited.add(logicalId);
  }

  logicalIds.forEach(visit);
}

function assertAllExplicitKmsReferencesUse(value, keyLogicalId, path = "template") {
  if (Array.isArray(value)) {
    value.forEach((item, index) =>
      assertAllExplicitKmsReferencesUse(item, keyLogicalId, `${path}[${index}]`)
    );
    return;
  }
  if (!value || typeof value !== "object") return;

  const kmsProperties = new Set([
    "KMSMasterKeyID",
    "KmsKeyArn",
    "KmsKeyId",
    "KmsKeyIdentifier",
    "KmsMasterKeyId",
  ]);
  for (const [name, child] of Object.entries(value)) {
    if (kmsProperties.has(name) && child?.["Fn::GetAtt"]?.[0] !== keyLogicalId) {
      fail(`${path}.${name} does not reference the one shared production key`);
    }
    assertAllExplicitKmsReferencesUse(child, keyLogicalId, `${path}.${name}`);
  }
}

function assertDev(tpl) {
  assertNoCircularResourceDependencies(tpl, "dev");
  if (resourcesOfType(tpl, "AWS::KMS::Key").length !== 0) {
    fail("dev must synthesize zero AWS::KMS::Key resources");
  }
  const serialized = JSON.stringify(tpl);
  if (/KmsKey|KMSMasterKey|KmsMasterKey|CUSTOMER_MANAGED_KMS_KEY/.test(serialized)) {
    fail("dev still contains a customer-managed KMS property");
  }

  for (const [, queue] of resourcesOfType(tpl, "AWS::SQS::Queue")) {
    if (queue.Properties.SqsManagedSseEnabled !== true) {
      fail("every dev SQS queue must use SQS-managed encryption");
    }
  }
  for (const [, bucket] of resourcesOfType(tpl, "AWS::S3::Bucket")) {
    const configs =
      bucket.Properties.BucketEncryption?.ServerSideEncryptionConfiguration || [];
    if (!configs.some((c) => c.ServerSideEncryptionByDefault?.SSEAlgorithm === "AES256")) {
      fail("the dev document bucket must use SSE-S3 (AES256)");
    }
  }
  for (const [, machine] of resourcesOfType(tpl, "AWS::StepFunctions::StateMachine")) {
    if (machine.Properties.EncryptionConfiguration?.Type !== "AWS_OWNED_KEY") {
      fail("the dev state machine must use AWS_OWNED_KEY");
    }
  }
}

function assertProd(tpl) {
  assertNoCircularResourceDependencies(tpl, "prod");
  const keys = resourcesOfType(tpl, "AWS::KMS::Key");
  if (keys.length !== 1) fail(`prod must synthesize one KMS key, found ${keys.length}`);
  const [keyLogicalId, key] = keys[0];
  if (key.DeletionPolicy !== "Retain" || key.UpdateReplacePolicy !== "Retain") {
    fail("the retained production data key must also use RETAIN");
  }

  const buckets = resourcesOfType(tpl, "AWS::S3::Bucket");
  if (buckets.length !== 1) fail(`expected one document bucket, found ${buckets.length}`);
  const bucketEncryption =
    buckets[0][1].Properties.BucketEncryption.ServerSideEncryptionConfiguration[0];
  const bucketKey = bucketEncryption.ServerSideEncryptionByDefault.KMSMasterKeyID;
  if (
    bucketEncryption.ServerSideEncryptionByDefault.SSEAlgorithm !== "aws:kms" ||
    bucketKey?.["Fn::GetAtt"]?.[0] !== keyLogicalId
  ) {
    fail("the production bucket does not reuse PipelineEncryptionKey");
  }

  const otherKmsKeys = new Set(
    Object.entries(tpl.Resources)
      .filter(([, resource]) => resource.Type === "AWS::KMS::Key")
      .map(([logicalId]) => logicalId)
  );
  if (otherKmsKeys.size !== 1 || !otherKmsKeys.has(keyLogicalId)) {
    fail("a production resource introduced an additional KMS key");
  }
  assertAllExplicitKmsReferencesUse(tpl.Resources, keyLogicalId);

  for (const [, machine] of resourcesOfType(tpl, "AWS::StepFunctions::StateMachine")) {
    const encryption = machine.Properties.EncryptionConfiguration;
    if (
      encryption?.Type !== "CUSTOMER_MANAGED_KMS_KEY" ||
      encryption.KmsKeyId?.["Fn::GetAtt"]?.[0] !== keyLogicalId
    ) {
      fail("the production state machine does not use the shared key");
    }
  }
}

synth("DocumentValidationStack-Dev", "dev");
assertDev(template("DocumentValidationStack-Dev.template.json"));
synth("DocumentValidationStack", "prod");
assertProd(template("DocumentValidationStack.template.json"));
console.log("✅ Encryption contract verified: dev=0 CMKs, prod=1 shared CMK.");
