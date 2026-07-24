#!/usr/bin/env node
/**
 * Track: Monitorea una ejecución del pipeline en tiempo real.
 *
 * Muestra paso a paso por dónde va el documento: SQS → Classification → Extraction → Enrichment → Done
 *
 * Uso:
 *   pnpm track --execution-arn <ARN>
 *   pnpm track --document-id curp-1777998616520
 *   pnpm track --latest
 *   pnpm track --follow   (espera y muestra la última ejecución en tiempo real)
 *
 * Opciones:
 *   --execution-arn   ARN completo de la ejecución de Step Functions
 *   --document-id     Busca la ejecución por document ID
 *   --latest          Muestra la ejecución más reciente
 *   --follow          Polling cada 3s hasta que termine
 *   --env dev|prod    Ambiente a monitorear (default: dev, igual que el deploy)
 *   --stack <NOMBRE>  Stack explícito (gana sobre --env)
 *   --region          AWS region (default: AWS_REGION o us-east-1)
 *   --help            Mostrar ayuda
 */

import { execSync } from "child_process";

const STATE_MACHINE_PREFIX = "BedrockDocumentProcessingbedrockdocumentprocessingworkflow";
// Un stack por ambiente (ver bin/app.ts). Ambos crean una state machine con el
// mismo prefijo de nombre, así que hay que desempatar por stack.
const DEV_STACK = "DocumentValidationStack-Dev";
const PROD_STACK = "DocumentValidationStack";

function parseArgs(args) {
  const parsed = {};
  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith("--")) {
      const key = args[i].slice(2);
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

function run(cmd, silent = false) {
  try {
    return execSync(cmd, { encoding: "utf8", timeout: 15000 }).trim();
  } catch (e) {
    if (!silent) console.error(`  ⚠️  Command failed: ${cmd.substring(0, 80)}...`);
    return null;
  }
}

/**
 * State machines del pipeline en la cuenta, más nueva primero.
 *
 * OJO: hay una por ambiente (dev y prod) y el constructo les pone el MISMO
 * prefijo de nombre, así que esto SIEMPRE puede devolver varias — por eso se
 * pide JSON y no `--output text` (que las pegaría en una sola cadena).
 */
function listStateMachines(region) {
  const result = run(
    `aws stepfunctions list-state-machines --region ${region} ` +
      `--query "stateMachines[?contains(name,'${STATE_MACHINE_PREFIX}')].{arn:stateMachineArn,created:creationDate}" ` +
      `--output json`
  );
  if (!result) return [];
  try {
    return JSON.parse(result).sort((a, b) => new Date(b.created) - new Date(a.created));
  } catch {
    return [];
  }
}

/** ARN de la state machine que pertenece a un stack concreto, o null. */
function stateMachineOfStack(stackName, region) {
  const result = run(
    `aws cloudformation describe-stack-resources --stack-name ${stackName} --region ${region} ` +
      `--query "StackResources[?ResourceType=='AWS::StepFunctions::StateMachine'].PhysicalResourceId" ` +
      `--output json`,
    true // silencioso: que el stack no exista es un caso normal
  );
  if (!result) return null;
  try {
    return JSON.parse(result)[0] || null;
  } catch {
    return null;
  }
}

/**
 * Elige la state machine a monitorear.
 *
 * Orden: --stack explícito → --env dev|prod (default dev, igual que el deploy)
 * → si el stack no resuelve, la única que exista → error explicando cómo elegir.
 */
function resolveStateMachine(args, region) {
  if (args.stack) {
    const arn = stateMachineOfStack(args.stack, region);
    if (!arn) {
      console.error(`❌ El stack '${args.stack}' no existe o no tiene state machine.`);
      process.exit(1);
    }
    return arn;
  }

  const stackName = args.env === "prod" ? PROD_STACK : DEV_STACK;
  const fromStack = stateMachineOfStack(stackName, region);
  if (fromStack) return fromStack;

  const machines = listStateMachines(region);
  if (machines.length === 1) return machines[0].arn;
  if (machines.length > 1) {
    console.error(
      `❌ Hay ${machines.length} state machines del pipeline y el stack '${stackName}' no resolvió.\n` +
        "   Elige una con --stack <NOMBRE_DEL_STACK> o --execution-arn:"
    );
    for (const m of machines) console.error(`     · ${m.arn}  (creada ${m.created})`);
    process.exit(1);
  }
  return null;
}

function getLatestExecution(smArn, region) {
  const result = run(
    `aws stepfunctions list-executions --state-machine-arn "${smArn}" --max-results 1 --region ${region} --output json`
  );
  if (!result) return null;
  const data = JSON.parse(result);
  return data.executions?.[0] || null;
}

function findExecutionByDocId(smArn, docId, region) {
  const result = run(
    `aws stepfunctions list-executions --state-machine-arn "${smArn}" --max-results 20 --region ${region} --output json`
  );
  if (!result) return null;
  const data = JSON.parse(result);
  return data.executions?.find((e) => e.name.includes(docId)) || null;
}

function getExecutionDetails(arn, region) {
  const result = run(
    `aws stepfunctions describe-execution --execution-arn "${arn}" --region ${region} --output json`
  );
  return result ? JSON.parse(result) : null;
}

function getExecutionHistory(arn, region) {
  const result = run(
    `aws stepfunctions get-execution-history --execution-arn "${arn}" --region ${region} --max-results 100 --output json`
  );
  return result ? JSON.parse(result) : null;
}

function formatDuration(startStr, endStr) {
  if (!startStr || !endStr) return "...";
  const ms = new Date(endStr) - new Date(startStr);
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function extractStepInfo(events) {
  const steps = [];
  const taskMap = {};

  for (const event of events) {
    const ts = event.timestamp;

    if (event.type === "TaskStateEntered") {
      const name = event.stateEnteredEventDetails?.name || "Unknown";
      taskMap[name] = { name, startedAt: ts, status: "⏳ running" };
    }

    if (event.type === "TaskStateExited") {
      const name = event.stateExitedEventDetails?.name || "Unknown";
      if (taskMap[name]) {
        taskMap[name].status = "✅ done";
        taskMap[name].endedAt = ts;
        // Try to extract output snippet
        try {
          const output = JSON.parse(event.stateExitedEventDetails?.output || "{}");
          if (output.documentClassification) {
            taskMap[name].detail = `→ ${output.documentClassification}`;
          } else if (output.classificationResult) {
            taskMap[name].detail = `→ ${output.classificationResult.documentClassification}`;
          } else if (output.validation) {
            taskMap[name].detail = `→ ${output.validation.status}`;
          }
        } catch {}
      }
    }

    if (event.type === "TaskFailed") {
      const error = event.taskFailedEventDetails?.error || "Unknown";
      const cause = event.taskFailedEventDetails?.cause || "";
      // Find which task this belongs to
      const lastTask = Object.values(taskMap).find((t) => t.status === "⏳ running");
      if (lastTask) {
        lastTask.status = "❌ failed";
        lastTask.error = error;
        lastTask.cause = cause.substring(0, 200);
        lastTask.endedAt = ts;
      }
    }

    if (event.type === "ExecutionSucceeded") {
      steps.push({ name: "_RESULT", status: "✅", ts });
      try {
        const output = JSON.parse(event.executionSucceededEventDetails?.output || "{}");
        steps[steps.length - 1].output = output;
      } catch {}
    }

    if (event.type === "ExecutionFailed") {
      steps.push({ name: "_FAILED", status: "❌", ts, error: event.executionFailedEventDetails });
    }
  }

  return Object.values(taskMap);
}

function printStatus(execution, steps) {
  const status = execution.status;
  const icon = status === "SUCCEEDED" ? "✅" : status === "FAILED" ? "❌" : "⏳";

  console.log(`\n${"═".repeat(70)}`);
  console.log(` ${icon} Ejecución: ${execution.name}`);
  console.log(`${"═".repeat(70)}`);
  console.log(`   Status:    ${status}`);
  console.log(`   Inicio:    ${execution.startDate}`);
  if (execution.stopDate) {
    console.log(`   Fin:       ${execution.stopDate}`);
    console.log(`   Duración:  ${formatDuration(execution.startDate, execution.stopDate)}`);
  }
  console.log();

  if (steps.length === 0) {
    console.log("   ⏳ Esperando primer paso...\n");
    return;
  }

  console.log("   Pasos:");
  for (const step of steps) {
    const duration = step.endedAt ? ` (${formatDuration(step.startedAt, step.endedAt)})` : "";
    const detail = step.detail ? ` ${step.detail}` : "";
    console.log(`     ${step.status} ${step.name}${duration}${detail}`);
    if (step.error) {
      console.log(`        Error: ${step.error}`);
      if (step.cause) console.log(`        Causa: ${step.cause}`);
    }
  }
  console.log();
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function printHelp() {
  console.log(`
Uso: pnpm track [opciones]

Monitorea una ejecución del pipeline de validación documental.

Opciones:
  --execution-arn   ARN de la ejecución de Step Functions
  --document-id     Buscar ejecución por document ID
  --latest          Mostrar la ejecución más reciente
  --follow          Polling cada 3s hasta que termine
  --env dev|prod    Ambiente a monitorear (default: dev)
  --stack <NOMBRE>  Stack explícito (gana sobre --env)
  --region          AWS region (default: us-east-1)

Ejemplos:
  pnpm track --latest
  pnpm track --latest --follow
  pnpm track -- --env prod --latest
  pnpm track --document-id curp-1777998616520
  pnpm track --execution-arn arn:aws:states:us-east-1:...
`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args.help) { printHelp(); process.exit(0); }

  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const follow = !!args.follow;

  // Find state machine (dev por default; --env prod o --stack para elegir)
  const smArn = resolveStateMachine(args, region);
  if (!smArn) {
    console.error("❌ No se encontró la state machine del pipeline.");
    console.error("   Verifica que el stack está desplegado.");
    process.exit(1);
  }
  if (!args["execution-arn"]) {
    console.log(`🔎 State machine: ${smArn.split(":").pop()}`);
  }

  // Resolve execution ARN
  let executionArn = args["execution-arn"];

  if (!executionArn && args["document-id"]) {
    const exec = findExecutionByDocId(smArn, args["document-id"], region);
    if (!exec) {
      console.error(`❌ No se encontró ejecución para document-id: ${args["document-id"]}`);
      process.exit(1);
    }
    executionArn = exec.executionArn;
  }

  if (!executionArn && (args.latest || !args["execution-arn"])) {
    const exec = getLatestExecution(smArn, region);
    if (!exec) {
      console.error("❌ No hay ejecuciones recientes.");
      process.exit(1);
    }
    executionArn = exec.executionArn;
  }

  if (!executionArn) {
    console.error("❌ Especifica --execution-arn, --document-id, o --latest\n");
    printHelp();
    process.exit(1);
  }

  // Track
  let lastStatus = "";
  do {
    const execution = getExecutionDetails(executionArn, region);
    if (!execution) {
      console.error("❌ No se pudo obtener la ejecución.");
      process.exit(1);
    }

    const history = getExecutionHistory(executionArn, region);
    const steps = history ? extractStepInfo(history.events || []) : [];

    // Clear screen on follow mode
    if (follow && lastStatus) {
      process.stdout.write("\x1B[2J\x1B[0f");
    }

    printStatus(execution, steps);

    lastStatus = execution.status;

    if (execution.status === "RUNNING" && follow) {
      process.stdout.write("   🔄 Polling en 3s... (Ctrl+C para salir)\r");
      await sleep(3000);
    } else {
      break;
    }
  } while (lastStatus === "RUNNING");

  // If succeeded, show final output
  if (lastStatus === "SUCCEEDED") {
    const execution = getExecutionDetails(executionArn, region);
    if (execution?.output) {
      try {
        const output = JSON.parse(execution.output);
        if (output.enrichedResult || output.processingResult) {
          console.log("   📋 Resultado final:");
          console.log(JSON.stringify(output.enrichedResult || output.processingResult, null, 2));
        }
      } catch {}
    }
  }
}

main();
