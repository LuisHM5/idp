#!/usr/bin/env node
/**
 * Test E2E: Sube un documento real al endpoint desplegado y muestra el resultado.
 *
 * Flujo completo: archivo local → base64 → POST /upload → espera procesamiento → consulta resultado
 *
 * Uso:
 *   pnpm test-e2e --file ./samples/ine_frente.pdf
 *   pnpm test-e2e --file ./samples/csf.pdf --endpoint https://xxx.execute-api.us-east-1.amazonaws.com/prod
 *   pnpm test-e2e --file ./samples/ine.jpg --wait 30
 *
 * Opciones:
 *   --file       (requerido) Ruta al documento a subir (jpg, jpeg, png, pdf)
 *   --endpoint   URL del API Gateway (default: lee de CloudFormation output)
 *   --stack      Nombre del stack CDK para obtener endpoint (default: "BedrockDocumentProcessingStack")
 *   --wait       Segundos a esperar por el resultado (default: 20)
 *   --region     AWS region (default: AWS_REGION env o "us-east-1")
 *   --no-wait    No esperar resultado, solo subir
 *   --help       Mostrar ayuda
 */

import { readFileSync, existsSync } from "fs";
import { basename, extname } from "path";
import { execSync } from "child_process";

function parseArgs(args) {
  const parsed = {};
  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith("--")) {
      const key = args[i].slice(2);
      if (key === "no-wait") { parsed.noWait = true; continue; }
      if (i + 1 >= args.length || args[i + 1].startsWith("--")) {
        parsed[key] = true;
      } else {
        parsed[key] = args[i + 1];
        i++;
      }
    }
  }
  return parsed;
}

function printHelp() {
  console.log(`
Uso: pnpm test-e2e --file <ruta_documento> [opciones]

Sube un documento al endpoint desplegado y muestra el resultado del pipeline completo.

Opciones:
  --file       (requerido) Ruta al documento (jpg, jpeg, png, pdf)
  --endpoint   URL del API Gateway (si no se da, se obtiene del stack CDK)
  --stack      Nombre del stack CDK (default: "BedrockDocumentProcessingStack")
  --wait       Segundos a esperar resultado (default: 20)
  --region     AWS region
  --no-wait    Solo subir, no esperar resultado
  --help       Mostrar ayuda

Ejemplos:
  pnpm test-e2e --file ./samples/ine_frente.pdf
  pnpm test-e2e --file ./samples/csf.jpg --wait 30
  pnpm test-e2e --file ./samples/formato.pdf --endpoint https://abc123.execute-api.us-east-1.amazonaws.com/prod
`);
}

function getEndpointFromStack(stackName, region) {
  try {
    const output = execSync(
      `aws cloudformation describe-stacks --stack-name "${stackName}" --region ${region} --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text`,
      { encoding: "utf8", timeout: 10000 }
    ).trim();
    return output || null;
  } catch {
    return null;
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args.help) { printHelp(); process.exit(0); }

  if (!args.file) {
    console.error("❌ --file es requerido\n");
    printHelp();
    process.exit(1);
  }

  if (!existsSync(args.file)) {
    console.error(`❌ Archivo no encontrado: ${args.file}`);
    process.exit(1);
  }

  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const stackName = args.stack || "BedrockDocumentProcessingStack";
  const waitSeconds = parseInt(args.wait || "20", 10);

  // Get endpoint
  let endpoint = args.endpoint;
  if (!endpoint) {
    console.log(`\n🔍 Obteniendo endpoint del stack '${stackName}'...`);
    endpoint = getEndpointFromStack(stackName, region);
    if (!endpoint) {
      console.error("❌ No se pudo obtener el endpoint. Usa --endpoint <url> o verifica el stack.");
      process.exit(1);
    }
  }

  // Ensure endpoint ends properly
  const uploadUrl = endpoint.endsWith("/")
    ? `${endpoint}upload`
    : `${endpoint}/upload`;

  // Read and encode file
  const fileName = basename(args.file);
  const ext = extname(fileName).slice(1).toLowerCase();
  const allowedExts = ["jpg", "jpeg", "png", "pdf"];

  if (!allowedExts.includes(ext)) {
    console.error(`❌ Formato '${ext}' no soportado. Permitidos: ${allowedExts.join(", ")}`);
    process.exit(1);
  }

  const fileBuffer = readFileSync(args.file);
  const contentBase64 = fileBuffer.toString("base64");

  console.log(`\n📄 Archivo:   ${fileName} (${(fileBuffer.length / 1024).toFixed(1)} KB)`);
  console.log(`🌐 Endpoint:  ${uploadUrl}`);

  // Upload
  console.log(`\n⏳ Subiendo documento...`);

  const body = JSON.stringify({
    fileName,
    contentBase64,
    metadata: { source: "test-e2e", uploadedAt: new Date().toISOString() },
  });

  try {
    const response = execSync(
      `curl -s -w "\\n%{http_code}" -X POST "${uploadUrl}" -H "Content-Type: application/json" -d @-`,
      {
        input: body,
        encoding: "utf8",
        timeout: 30000,
      }
    );

    const lines = response.trim().split("\n");
    const httpCode = lines.pop();
    const responseBody = lines.join("\n");

    if (httpCode !== "200") {
      console.error(`\n❌ Upload falló (HTTP ${httpCode}):`);
      console.error(responseBody);
      process.exit(1);
    }

    const uploadResult = JSON.parse(responseBody);
    console.log(`\n✅ Upload exitoso:`);
    console.log(`   Document ID: ${uploadResult.documentId}`);
    console.log(`   S3 Key:      ${uploadResult.key}`);
    console.log(`   Timestamp:   ${uploadResult.uploadedAt}`);

    if (args.noWait) {
      console.log(`\n   (--no-wait: no se esperará el resultado del pipeline)\n`);
      return;
    }

    // Wait for processing
    console.log(`\n⏳ Esperando procesamiento (${waitSeconds}s)...`);
    console.log(`   El pipeline ejecuta: Clasificación → Extracción → Validación`);

    for (let i = 0; i < waitSeconds; i++) {
      await sleep(1000);
      process.stdout.write(`\r   ${i + 1}/${waitSeconds}s`);
    }
    console.log("\n");

    // Try to get result from DynamoDB (the construct stores results there)
    console.log(`🔍 Buscando resultado en DynamoDB...`);
    try {
      const scanResult = execSync(
        `aws dynamodb scan --table-name "bedrock-document-processing" --region ${region} --filter-expression "documentId = :id" --expression-attribute-values '{":id":{"S":"${uploadResult.documentId}"}}' --output json 2>/dev/null`,
        { encoding: "utf8", timeout: 10000 }
      );
      const data = JSON.parse(scanResult);
      if (data.Items && data.Items.length > 0) {
        console.log(`\n✅ Resultado encontrado:\n`);
        console.log(JSON.stringify(data.Items[0], null, 2));
      } else {
        console.log(`\n⚠️  Resultado aún no disponible en DynamoDB.`);
        console.log(`   El pipeline puede tardar más. Intenta con --wait ${waitSeconds + 20}`);
        console.log(`   O revisa los logs de Step Functions en la consola AWS.`);
      }
    } catch {
      console.log(`\n⚠️  No se pudo consultar DynamoDB directamente.`);
      console.log(`   Revisa el resultado en la consola AWS:`);
      console.log(`   - Step Functions: busca la ejecución con documentId ${uploadResult.documentId}`);
      console.log(`   - EventBridge: busca eventos de 'document-validation-pipeline'`);
    }

  } catch (e) {
    console.error(`\n❌ Error en la petición:`);
    console.error(e.message);
    process.exit(1);
  }
}

main();
