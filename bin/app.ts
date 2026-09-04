#!/usr/bin/env node
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { BedrockDocumentProcessingStack } from "../src/document-validation-stack";

const app = new cdk.App();

// Environment: `cdk deploy -c env=dev|prod`. Default is DEV (safe: isolated
// stack, open API), so a bare `pnpm run deploy` never touches prod. Prod is
// explicit via `pnpm run deploy:prod` (-c env=prod).
const environment = (app.node.tryGetContext("env") as string) === "prod" ? "prod" : "dev";

// Load the matching env file (does NOT override existing process env vars).
const envFile = path.join(
  __dirname,
  environment === "prod" ? "../.env.production" : "../.env.dev"
);
if (fs.existsSync(envFile)) {
  for (const line of fs.readFileSync(envFile, "utf-8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eqIdx = trimmed.indexOf("=");
    if (eqIdx === -1) continue;
    const key = trimmed.slice(0, eqIdx).trim();
    const value = trimmed.slice(eqIdx + 1).trim().replace(/^["']|["']$/g, "");
    if (!process.env[key]) process.env[key] = value;
  }
}

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION || "us-east-1",
};

// prod keeps the original stack id (in-place update); dev is an isolated stack.
const stackId =
  environment === "prod" ? "DocumentValidationStack" : "DocumentValidationStack-Dev";

new BedrockDocumentProcessingStack(app, stackId, {
  env,
  environment,
  // Stack metadata only: CloudFormation propagates this tag to every managed
  // resource type that supports stack-level tags. Keeping it in StackProps
  // also tags the stack itself without changing names, references or behavior.
  tags: { CostStack: stackId },
  enableDynamoCatalog: true,
  forceReloadCatalog: true,
  enableFallbackModels: true,
  // Custom domain applies in prod only; unset in dev → random execute-api URL.
  domainName: environment === "prod" ? process.env.DOMAIN_NAME : undefined,
  certArn: environment === "prod" ? process.env.CERT_ARN : undefined,
  // Dev-only toggle to require x-api-key like prod; ignored when prod (always
  // required there regardless of this value).
  devApiKeyRequired:
    environment !== "prod" &&
    (process.env.DEV_API_KEY_REQUIRED || "").trim().toLowerCase() === "true",
});
