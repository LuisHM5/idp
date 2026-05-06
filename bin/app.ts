#!/usr/bin/env node
import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { BedrockDocumentProcessingStack } from "../src/document-validation-stack";

// Load .env.production if it exists (does NOT override existing env vars)
const envFile = path.join(__dirname, "../.env.production");
if (fs.existsSync(envFile)) {
  const lines = fs.readFileSync(envFile, "utf-8").split("\n");
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eqIdx = trimmed.indexOf("=");
    if (eqIdx === -1) continue;
    const key = trimmed.slice(0, eqIdx).trim();
    const value = trimmed.slice(eqIdx + 1).trim().replace(/^["']|["']$/g, "");
    if (!process.env[key]) {
      process.env[key] = value;
    }
  }
}

const app = new cdk.App();

new BedrockDocumentProcessingStack(app, "DocumentValidationStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION || "us-east-1",
  },
  enableDynamoCatalog: true,
  forceReloadCatalog: true,
});
