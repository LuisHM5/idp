#!/usr/bin/env node
/**
 * Test local: Simula el flujo de validación sin S3/EventBridge/Step Functions.
 *
 * Ejecuta la misma lógica que la Lambda de enrichment directamente con un
 * documento clasificado y entidades simuladas o proporcionadas.
 *
 * Uso:
 *   pnpm test-local --type INE --entities '{"CURP":"PELJ850101HDFRPN09","VIGENCIA":"2029-12-31","NOMBRE_COMPLETO":"Juan Pérez","NUMERO_DOCUMENTO":"1234567890"}'
 *   pnpm test-local --type CONSTANCIA_SITUACION_FISCAL --entities '{"RFC":"XAXX010101000","NOMBRE_O_RAZON_SOCIAL":"Empresa SA"}'
 *   pnpm test-local --type INE  (usa entidades de ejemplo)
 *   pnpm test-local             (modo interactivo)
 *
 * Opciones:
 *   --type       Tipo documental (clasificación simulada)
 *   --entities   JSON object con entidades extraídas
 *   --file       Ruta a un JSON con el evento completo (simula Step Functions input)
 *   --help       Mostrar ayuda
 */

import { execSync } from "child_process";
import { readFileSync, existsSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import { createInterface } from "readline";

const __dirname = dirname(fileURLToPath(import.meta.url));
const LAMBDA_DIR = resolve(
  __dirname,
  "../src/document-processing/resources/enrichment-lambda"
);

// Sample entities for quick testing
const SAMPLE_ENTITIES = {
  INE: {
    NOMBRE_COMPLETO: "Juan Pérez López",
    NOMBRE: "Juan",
    APELLIDO_PATERNO: "Pérez",
    APELLIDO_MATERNO: "López",
    CURP: "PELJ850101HDFRPN09",
    NUMERO_DOCUMENTO: "1234567890123",
    VIGENCIA: "2029-12-31",
    FECHA_NACIMIENTO: "01/01/1985",
    DOMICILIO: "Calle Falsa 123, CDMX",
    TIPO_DOCUMENTO: "INE",
  },
  PASAPORTE: {
    NOMBRE_COMPLETO: "María García Hernández",
    CURP: "GAHM900215MDFRRL08",
    NUMERO_DOCUMENTO: "G12345678",
    VIGENCIA: "2028-06-15",
    FECHA_NACIMIENTO: "15/02/1990",
  },
  CONSTANCIA_SITUACION_FISCAL: {
    RFC: "XAXX010101000",
    CURP: "XAXX010101HDFRRX09",
    NOMBRE_O_RAZON_SOCIAL: "Empresa Demo SA de CV",
    REGIMEN_FISCAL: "601 - General de Ley",
    CODIGO_POSTAL: "06600",
    FECHA_EMISION: "2025-01-15",
    ESTATUS_EN_PADRON: "ACTIVO",
    ACTIVIDAD_ECONOMICA: "Comercio al por menor",
  },
  FORMATO_INTERNO_ALTA: {
    TIPO_FORMATO: "ALTA",
    FOLIO_INTERNO: "ALT-2025-001",
    NOMBRE_SOLICITANTE: "Carlos Ruiz",
    RFC: "RUIC880305AB1",
    CURP: "RUIC880305HDFRRL09",
    FECHA_SOLICITUD: "2025-05-01",
    AREA: "Recursos Humanos",
    FIRMA_PRESENTE: "SI",
    CAMPOS_OBLIGATORIOS_COMPLETOS: "SI",
    VERSION_FORMATO: "V2.1",
  },
  OTRO: {
    TEXTO_GENERICO: "Documento no identificado",
  },
};

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

function printHelp() {
  console.log(`
Uso: pnpm test-local [opciones]

Simula el flujo de validación localmente (sin AWS).

Opciones:
  --type       Tipo documental (ej: INE, CONSTANCIA_SITUACION_FISCAL)
  --entities   JSON con entidades extraídas
  --file       Ruta a JSON con evento completo de Step Functions
  --help       Mostrar ayuda

Ejemplos:
  # Con entidades de ejemplo predefinidas
  pnpm test-local --type INE

  # Con entidades custom
  pnpm test-local --type INE --entities '{"CURP":"PELJ850101HDFRPN09","VIGENCIA":"2020-01-01"}'

  # Con archivo de evento
  pnpm test-local --file test-events/ine_valida.json

  # Interactivo
  pnpm test-local

Tipos con datos de ejemplo: ${Object.keys(SAMPLE_ENTITIES).join(", ")}
`);
}

function buildEvent(classification, entities, documentId = "test-local-001") {
  const entitiesList = Object.entries(entities).map(([type, value]) => ({
    type,
    value,
  }));

  return {
    documentId,
    classificationResult: { documentClassification: classification },
    processingResult: { result: { entities: entitiesList } },
  };
}

function runValidation(event) {
  // Run the Python handler locally
  const script = `
import sys, json
sys.path.insert(0, '${LAMBDA_DIR}')
from handler import handler
event = json.loads('''${JSON.stringify(event)}''')
result = handler(event)
print(json.dumps(result, indent=2, ensure_ascii=False))
`;

  try {
    const output = execSync(`python3 -c "${script.replace(/"/g, '\\"')}"`, {
      encoding: "utf8",
      timeout: 15000,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    });
    return JSON.parse(output);
  } catch (e) {
    // Try with inline heredoc approach
    const output2 = execSync(`python3 << 'PYEOF'
import sys, json
sys.path.insert(0, '${LAMBDA_DIR}')
from handler import handler
event = json.loads('${JSON.stringify(event).replace(/'/g, "\\'")}')
result = handler(event)
print(json.dumps(result, indent=2, ensure_ascii=False))
PYEOF`, {
      encoding: "utf8",
      timeout: 15000,
      shell: "/bin/bash",
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    });
    return JSON.parse(output2);
  }
}

function printResult(result) {
  const status = result.validation?.status;
  const icon = status === "APROBADO" ? "✅" : status === "RECHAZADO" ? "❌" : "⚠️";

  console.log(`\n${"═".repeat(70)}`);
  console.log(` ${icon} RESULTADO: ${status}`);
  console.log(`${"═".repeat(70)}`);
  console.log(`\n   Documento:      ${result.documentId}`);
  console.log(`   Clasificación:  ${result.documentClassification}`);
  console.log(`   Procesado:      ${result.processedAt}`);

  console.log(`\n   Entidades extraídas:`);
  for (const [k, v] of Object.entries(result.extractedEntities || {})) {
    console.log(`     ${k}: ${v}`);
  }

  console.log(`\n   Validaciones (${result.validation?.checks?.length || 0}):`);
  for (const check of result.validation?.checks || []) {
    const ci = check.valid ? "✓" : "✗";
    console.log(`     ${ci} [${check.rule}] ${check.field}: ${check.message}`);
  }

  console.log(`\n   Resumen: ${result.validation?.summary}`);
  console.log();
}

async function interactiveMode() {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  const ask = (q) => new Promise((r) => rl.question(q, (a) => r(a.trim())));

  console.log("\n🧪 Test local de validación (modo interactivo)\n");
  console.log(`   Tipos disponibles: ${Object.keys(SAMPLE_ENTITIES).join(", ")}\n`);

  const type = (await ask("Tipo documental: ")).toUpperCase();
  if (!type) { rl.close(); process.exit(0); }

  const useDefault = SAMPLE_ENTITIES[type];
  let entities;

  if (useDefault) {
    const raw = await ask(`¿Usar entidades de ejemplo para ${type}? [S/n]: `);
    if (!raw || raw.toLowerCase() === "s" || raw.toLowerCase() === "si") {
      entities = useDefault;
    }
  }

  if (!entities) {
    const raw = await ask("Entidades (JSON): ");
    try { entities = JSON.parse(raw); }
    catch { console.error("❌ JSON inválido"); rl.close(); process.exit(1); }
  }

  rl.close();

  const event = buildEvent(type, entities);
  console.log("\n⏳ Ejecutando validación...");
  const result = runValidation(event);
  printResult(result);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args.help) { printHelp(); process.exit(0); }

  // Flag mode (--type takes priority over --file)
  if (args.type) {
    const type = args.type.toUpperCase();
    let entities;

    if (args.entities) {
      try { entities = JSON.parse(args.entities); }
      catch { console.error("❌ --entities JSON inválido"); process.exit(1); }
    } else {
      entities = SAMPLE_ENTITIES[type];
      if (!entities) {
        console.error(`❌ No hay entidades de ejemplo para '${type}'. Usa --entities '{...}'`);
        process.exit(1);
      }
      console.log(`   Usando entidades de ejemplo para ${type}`);
    }

    const event = buildEvent(type, entities);
    console.log("\n⏳ Ejecutando validación...");
    const result = runValidation(event);
    printResult(result);
    return;
  }

  // File mode
  if (args.file) {
    if (!existsSync(args.file)) {
      console.error(`❌ Archivo no encontrado: ${args.file}`);
      process.exit(1);
    }
    const ext = args.file.split(".").pop().toLowerCase();
    if (["pdf", "jpg", "jpeg", "png"].includes(ext)) {
      console.error(`\n❌ test-local no procesa documentos reales (${ext}).`);
      console.error(`   Este script simula la validación con entidades ya extraídas.\n`);
      console.error(`   Para probar con un documento real, usa test-e2e:`);
      console.error(`     pnpm test-e2e --file ${args.file}\n`);
      console.error(`   Para test-local, usa un JSON de evento:`);
      console.error(`     pnpm test-local --file test-events/ine_valida.json`);
      console.error(`     pnpm test-local --type INE --entities '{"CURP":"..."}'\n`);
      process.exit(1);
    }
    const event = JSON.parse(readFileSync(args.file, "utf8"));
    console.log("\n⏳ Ejecutando validación desde archivo...");
    const result = runValidation(event);
    printResult(result);
    return;
  }

  // Interactive mode
  if (Object.keys(args).length === 0) {
    await interactiveMode();
    return;
  }

  console.error("❌ Usa --type o --file, o ejecuta sin flags para modo interactivo.");
  process.exit(1);
}

main();
