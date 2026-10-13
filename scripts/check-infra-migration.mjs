#!/usr/bin/env node
/**
 * Deployment gate for the catalog-blueprints removal.
 *
 * These logical ids identify resources that already hold data or sit on the
 * live ingestion path. A refactor that changes one must be handled as an
 * explicit CloudFormation import/migration, never as an ordinary deploy.
 */
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
const expected = {
  IDPBrokerEventBus3145175C: "AWS::Events::EventBus",
  BedrockDocumentProcessingDocumentProcessingBucket32BD72F9: "AWS::S3::Bucket",
  BedrockDocumentProcessingDocumentProcessingDLQ21393F89: "AWS::SQS::Queue",
  BedrockDocumentProcessingDocumentProcessingQueue7C8A9064: "AWS::SQS::Queue",
  BedrockDocumentProcessingDocumentProcessingTableD87A7B20: "AWS::DynamoDB::Table",
  BedrockDocumentProcessingbedrockdocumentprocessingworkflow1851BBD2:
    "AWS::StepFunctions::StateMachine",
};

for (const [logicalId, type] of Object.entries(expected)) {
  const resource = template.Resources?.[logicalId];
  if (!resource || resource.Type !== type) {
    throw new Error(
      `Migración insegura: falta ${logicalId} (${type}). ` +
        "Detén el deploy y prepara retain/import explícito."
    );
  }
}

const mappings = Object.values(template.Resources).filter(
  (resource) => resource.Type === "AWS::Lambda::EventSourceMapping"
);
if (
  mappings.length !== 1 ||
  mappings[0].Properties?.MaximumBatchingWindowInSeconds !== 0 ||
  mappings[0].Properties?.FunctionResponseTypes?.[0] !== "ReportBatchItemFailures"
) {
  throw new Error(
    "El ingress debe tener una sola fuente SQS, ventana 0 y fallas parciales habilitadas"
  );
}

const stateMachine =
  template.Resources[
    "BedrockDocumentProcessingbedrockdocumentprocessingworkflow1851BBD2"
  ];
const definition = JSON.stringify(stateMachine.Properties);
for (const state of [
  "SelectCatalogPipelineRoute",
  "PrepareDeclaredClassification",
  "EnrichmentStep-0",
  "EnrichmentStep-1",
]) {
  if (!definition.includes(state)) {
    throw new Error(`La state machine sintetizada perdió el estado requerido ${state}`);
  }
}
for (const state of ["EnrichmentStep-0", "EnrichmentStep-1"]) {
  const start = definition.indexOf(state);
  const taskFragment = definition.slice(start, start + 1000);
  if (taskFragment.includes("states:::lambda:invoke")) {
    throw new Error(
      `${state} perdió payloadResponseOnly: DynamoDB recibiría el envelope de Lambda`
    );
  }
}

function actionsFor(logicalId) {
  const statements =
    template.Resources?.[logicalId]?.Properties?.PolicyDocument?.Statement ?? [];
  return statements.flatMap((statement) =>
    Array.isArray(statement.Action) ? statement.Action : [statement.Action]
  );
}

const workflowActions = actionsFor(
  "BedrockDocumentProcessingbedrockdocumentprocessingworkflowRoleDefaultPolicy246F38EB"
);
if (
  !workflowActions.some((action) => action?.startsWith("s3:GetObject")) ||
  !workflowActions.includes("s3:PutObject") ||
  !workflowActions.some((action) => action?.startsWith("s3:DeleteObject"))
) {
  throw new Error(
    "La Step Function no tiene los permisos S3 completos para mover raw → processed/failed"
  );
}

const consumerActions = actionsFor(
  "BedrockDocumentProcessingSQSConsumerServiceRoleDefaultPolicyBE69D1F6"
);
if (consumerActions.some((action) => action?.startsWith("bedrock:"))) {
  throw new Error("El ingress SQS no debe tener permisos para invocar modelos Bedrock");
}

console.log(
  `Infra migration contract OK (${stackName}): recursos stateful conservados, ` +
    "un solo ingress, batching=0, rutas presentes y permisos mínimos verificados"
);
