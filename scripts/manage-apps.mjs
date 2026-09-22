#!/usr/bin/env node
/**
 * Registra y administra apps consumidoras de la API (una API key + usage
 * plan de API Gateway por app), sin necesitar `cdk deploy` para dar de alta
 * o de baja una app — a diferencia de la key única `idp-default` que sí
 * gestiona el stack CDK (ver src/document-validation-stack.ts). Cada app
 * queda registrada en una tabla DynamoDB (`document-validation-apps[-dev]`,
 * creada por el stack) que el Upload Lambda consulta para resolver el
 * `x-api-key` de cada request al nombre legible que viaja como `appOrigin`
 * en el JSON de resultado de cada documento.
 *
 * Uso:
 *   pnpm apps:register                                  (interactivo: pregunta lo que falte)
 *   pnpm apps:register -- --name CEDIS --env dev
 *   pnpm apps:register -- --name ERP --env prod --rate-limit 10 --burst-limit 20 \
 *        --read-rate-limit 40 --read-burst-limit 80 --docs-per-day 300 --quota-per-day 15000
 *   pnpm apps:rotate -- --name CEDIS --env dev          (key nueva, la vieja queda deshabilitada)
 *   pnpm apps:reconfigure -- --name CEDIS --env dev --read-rate-limit 20   (solo cambia límites, misma key)
 *   pnpm apps:reconfigure -- --name CEDIS --env dev --docs-per-day 50      (o `none` para quitar el tope)
 *   pnpm apps:reconfigure -- --name CEDIS --env dev --access-scope all --allow-field-search
 *   pnpm apps:grant -- --name ERP --env prod --metadata-write
 *   pnpm apps:reconfigure -- --name CEDIS --env dev --allowed-doctypes FACTURA_HONORARIOS_XML
 *   pnpm apps:grant -- --name CEDIS --env dev --doctype PEDIMENTO_ADUANAL   (agrega SIN pisar lo existente)
 *   pnpm apps:deny -- --name CEDIS --env dev --field-search                (quita SOLO ese permiso)
 *   pnpm apps:disable -- --name CEDIS --env dev         (deshabilita la key, no la borra)
 *   pnpm apps:enable -- --name CEDIS --env dev          (reactiva una app deshabilitada)
 *   pnpm apps:revoke -- --name CEDIS --env dev          (= disable)
 *   pnpm apps:revoke -- --name CEDIS --env dev --delete   (+ borra los recursos AWS, irreversible)
 *   pnpm apps:adopt -- --name legacy-idp-default --api-key-id <ApiKeyId> --env prod
 *   pnpm apps:list -- --env prod
 *   pnpm apps:usage -- --env dev [--name CEDIS] [--days 7]   (límites reales + consumo por día)
 *
 * Límites por app (detalle en scripts/_app-limits.mjs y
 * documentacion/AMBIENTES_Y_SEGURIDAD.md → "Límites por app"):
 *   --rate-limit / --burst-limit            throttle de ESCRITURA (POST /documents,
 *                                           /upload, reprocess, PATCH metadata).
 *   --read-rate-limit / --read-burst-limit  throttle de LECTURA (todos los GET);
 *                                           se recomienda 4× el de escritura.
 *   --docs-per-day N|none                   tope de documentos/día que cuenta la
 *                                           Lambda (solo POST /documents, /upload y
 *                                           reprocess); `none` = sin tope.
 *   --quota-per-day N                       techo de requests/día de API Gateway
 *                                           (cuenta TODO, incluido el polling).
 *
 * TODOS los comandos que mutan algo (register/rotate/reconfigure/grant/deny/
 * disable/enable/revoke/adopt — `list` y `usage` son de solo lectura) son interactivos cuando
 * corren en una terminal real y falta algún dato, y SIEMPRE terminan con una
 * confirmación S/n antes de tocar AWS — incluso si diste todos los flags,
 * salvo que pases `--yes` o no haya TTY (pipe/CI), en cuyo caso no preguntan
 * ni confirman nada y fallan si falta algo requerido (para scripting/CI):
 *   - `register`: si falta algún límite, pide el volumen de documentos
 *     esperado por día para RECOMENDAR valores (`recommendLimits`/
 *     `VOLUME_TIERS` en scripts/_app-limits.mjs).
 *   - `reconfigure`: busca la app primero y muestra sus límites ACTUALES como
 *     default de cada pregunta — Enter deja ese campo sin cambios, no repite
 *     la recomendación desde cero (aquí se edita algo que ya existe).
 *   - `revoke --delete` y `rotate` muestran una advertencia extra (son las
 *     acciones más difíciles/imposibles de deshacer) antes de confirmar.
 *
 * Permisos por app (ver documentacion/AMBIENTES_Y_SEGURIDAD.md → "Acceso por
 * app" — los aplica query-lambda en GET /documents* y PATCH metadata). Dos formas de
 * tocarlos, según si quieres REEMPLAZAR todo o solo AJUSTAR un permiso:
 *   - `register`/`reconfigure --allowed-doctypes A,B`: fija la lista EXACTA
 *     (pisa lo que hubiera). Para quitar la restricción: `--allowed-doctypes none`.
 *   - `grant`/`deny --doctype A,B`: suma/resta esos tipos a la lista actual
 *     SIN tocar el resto — no hace falta saber/retipear los que ya tenía.
 * En ambos casos, los mismos cuatro permisos:
 *   --access-scope own|all   own (default en register) = solo ve documentos
 *                             que ELLA subió (tracking.appOrigin); all = sin restricción.
 *   --allowed-doctypes A,B   (solo register/reconfigure) — ver arriba.
 *   --doctype A,B            (solo grant/deny) — ver arriba.
 *   --allow-field-search /
 *   --disallow-field-search  (register/reconfigure) permite/quita el
 *                             Scan-based GET /documents?field=&value=
 *                             (default false — la ruta más cara y abusable).
 *   --field-search            (grant/deny) mismo permiso, ya con el sentido
 *                             de "otorgar"/"quitar" fijado por el comando.
 *   --allow-metadata-write /
 *   --disallow-metadata-write (register/reconfigure) permite/quita
 *                             PATCH /documents/{id}/metadata (default false).
 *   --metadata-write          (grant/deny) otorga/quita ese mismo permiso.
 *
 * Opciones comunes:
 *   --env dev|prod     Ambiente (default: dev, igual que el deploy)
 *   --stack <NOMBRE>   Stack explícito (gana sobre --env)
 *   --table <NOMBRE>   Tabla Apps explícita (si no, se infiere de --env)
 *   --region           AWS region (default: AWS_REGION o us-east-1)
 *   --yes              No preguntar ni confirmar nada; usar flags/defaults tal cual — para scripting/CI
 *
 * Usa el AWS CLI vía execSync (mismo criterio que scripts/backfill-index.mjs
 * y scripts/import-cert.mjs) — sin dependencia npm extra, solo credenciales
 * con permisos de apigateway (ApiKey / UsagePlan / GetUsage) y dynamodb
 * (Scan / Query / PutItem / UpdateItem).
 */

import { execSync } from "child_process";
import { writeFileSync, unlinkSync } from "fs";
import { createInterface } from "node:readline/promises";
import {
  effectiveLimits,
  formatThrottle,
  parseLimit,
  recommendLimits,
  recommendReadLimits,
  usagePlanInput,
  usagePlanPatchOps,
} from "./_app-limits.mjs";

const DEV_STACK = "DocumentValidationStack-Dev";
const PROD_STACK = "DocumentValidationStack";

function parseArgs(argv) {
  const parsed = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith("--")) {
      const key = argv[i].slice(2);
      if (i + 1 >= argv.length || argv[i + 1].startsWith("--")) {
        parsed[key] = true;
      } else {
        parsed[key] = argv[i + 1];
        i++;
      }
    }
  }
  return parsed;
}

function fail(msg) {
  console.error(`❌ ${msg}`);
  process.exit(1);
}

/** Pregunta el nombre de la app; nunca deja pasar un valor vacío. */
async function promptName(rl, label) {
  const name = (await rl.question(`${label}: `)).trim();
  if (!name) fail("El nombre no puede quedar vacío.");
  return name;
}

/** Mismo prompt de ambiente en todos los comandos — default dev, igual que el deploy. */
async function promptEnv(rl) {
  const ans = (await rl.question("¿Ambiente? (dev/prod) [dev]: ")).trim().toLowerCase();
  return ans === "prod" ? "prod" : "dev";
}

/** Confirmación S/n compartida por todos los comandos que mutan algo — "n"/"no" cancela sin tocar AWS. */
async function confirmOrExit(rl, message) {
  const ans = (await rl.question(`${message} (S/n): `)).trim().toLowerCase();
  if (ans === "n" || ans === "no") {
    console.log("Cancelado.");
    process.exit(0);
  }
}

function run(cmd, region, opts = {}) {
  try {
    return execSync(`aws ${cmd} --region ${region}`, {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      maxBuffer: 64 * 1024 * 1024,
    }).trim();
  } catch (e) {
    if (opts.silent) return null;
    console.error(`❌ Comando AWS falló: aws ${cmd}`);
    console.error(e.stderr?.toString().trim() || e.message);
    process.exit(1);
  }
}

function resolveStack(args) {
  return args.stack || (args.env === "prod" ? PROD_STACK : DEV_STACK);
}

function resolveAppsTable(args) {
  if (args.table) return args.table;
  return `document-validation-apps${args.env === "prod" ? "" : "-dev"}`;
}

/** Contador de documentos/día por app que escriben las Lambdas (daily-doc-cap.mjs). */
function resolveUsageTable(args) {
  if (args["usage-table"]) return args["usage-table"];
  return `document-validation-app-usage${args.env === "prod" ? "" : "-dev"}`;
}

/** Mismo patrón que backfill-index.mjs::resolveTable, para el RestApi. */
function resolveRestApiId(stack, region) {
  const out = run(
    `cloudformation describe-stack-resources --stack-name ${stack} ` +
      `--query "StackResources[?ResourceType=='AWS::ApiGateway::RestApi'].PhysicalResourceId" --output json`,
    region
  );
  const ids = JSON.parse(out);
  if (!ids.length) {
    fail(`No encontré el RestApi en el stack ${stack}. ¿Está desplegado?`);
  }
  return ids[0];
}

function resolveStage(apiId, region) {
  return run(`apigateway get-stages --rest-api-id ${apiId} --query "item[0].stageName" --output text`, region);
}

function unmarshal(item) {
  const out = {};
  for (const [k, v] of Object.entries(item)) {
    if ("S" in v) out[k] = v.S;
    else if ("N" in v) out[k] = Number(v.N);
    else if ("SS" in v) out[k] = v.SS;
    else if ("BOOL" in v) out[k] = v.BOOL;
    else out[k] = v;
  }
  return out;
}

/**
 * Parsea los flags de permisos por app (compartidos por `register` y
 * `reconfigure`): --access-scope own|all, --allowed-doctypes A,B (o "none"
 * para quitar la restricción), --allow-field-search/--disallow-field-search
 * y --allow-metadata-write/--disallow-metadata-write.
 * `setAttrs` va directo a un put-item/update-item SET; `removeAttrs` son
 * nombres de atributo para un REMOVE (DynamoDB no permite un String Set
 * vacío, así que "quitar" AllowedDocTypes es un REMOVE, no un SS: []).
 */
function parsePermissionFlags(args) {
  const setAttrs = {};
  const removeAttrs = [];
  if (args["access-scope"] !== undefined) {
    const v = String(args["access-scope"]).trim().toLowerCase();
    if (v !== "own" && v !== "all") fail("--access-scope debe ser 'own' o 'all'.");
    setAttrs.AccessScope = { S: v };
  }
  if (args["allowed-doctypes"] !== undefined) {
    const raw = String(args["allowed-doctypes"]).trim();
    if (raw.toLowerCase() === "none") {
      removeAttrs.push("AllowedDocTypes");
    } else {
      const list = raw.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
      if (list.length) setAttrs.AllowedDocTypes = { SS: list };
    }
  }
  if (args["allow-field-search"]) setAttrs.AllowFieldSearch = { BOOL: true };
  if (args["disallow-field-search"]) setAttrs.AllowFieldSearch = { BOOL: false };
  if (args["allow-metadata-write"]) setAttrs.AllowMetadataWrite = { BOOL: true };
  if (args["disallow-metadata-write"]) setAttrs.AllowMetadataWrite = { BOOL: false };
  return { setAttrs, removeAttrs };
}

/** Scan completo (bajo volumen, uso administrativo — no hace falta un GSI). */
function scanApps(table, region) {
  let startKey = null;
  const items = [];
  do {
    const cmd =
      `dynamodb scan --table-name ${table} --no-paginate --output json` +
      (startKey ? ` --exclusive-start-key '${JSON.stringify(startKey)}'` : "");
    const page = JSON.parse(run(cmd, region));
    items.push(...(page.Items || []).map(unmarshal));
    startKey = page.LastEvaluatedKey || null;
  } while (startKey);
  return items;
}

function findAppByName(table, name, region, { activeOnly = false } = {}) {
  return scanApps(table, region).find(
    (it) =>
      it.AppName &&
      it.AppName.toLowerCase() === name.toLowerCase() &&
      (!activeOnly || it.Status === "ACTIVE")
  );
}

/**
 * `aws <cmd> --cli-input-json` vía archivo temporal: evita pelear con el
 * quoting del shell para payloads con llaves/comillas (items de DynamoDB,
 * patch paths de usage plans como `/apiStages/x:prod/throttle/documents/{id}/...`).
 */
function cliJson(cmd, payload, region, opts = {}) {
  const tmp = `/tmp/aws-apps-${Date.now()}-${Math.random().toString(36).slice(2)}.json`;
  writeFileSync(tmp, JSON.stringify(payload));
  try {
    return run(`${cmd} --cli-input-json file://${tmp} --output json`, region, opts);
  } finally {
    unlinkSync(tmp);
  }
}

function ddbWrite(op, payload, region) {
  cliJson(`dynamodb ${op}`, payload, region);
}

function ddbPutItem(table, item, region) {
  ddbWrite("put-item", { TableName: table, Item: item }, region);
}

function ddbUpdateItem(table, key, attrs, region) {
  const names = {};
  const values = {};
  const sets = [];
  Object.entries(attrs).forEach(([k, v], i) => {
    names[`#a${i}`] = k;
    values[`:a${i}`] = v;
    sets.push(`#a${i} = :a${i}`);
  });
  ddbWrite(
    "update-item",
    {
      TableName: table,
      Key: key,
      UpdateExpression: `SET ${sets.join(", ")}`,
      ExpressionAttributeNames: names,
      ExpressionAttributeValues: values,
    },
    region
  );
}

/** REMOVE uno o más atributos — para "quitar" AllowedDocTypes (ver parsePermissionFlags). */
function ddbRemoveAttrs(table, key, attrNames, region) {
  if (!attrNames.length) return;
  const names = {};
  const removes = [];
  attrNames.forEach((name, i) => {
    names[`#r${i}`] = name;
    removes.push(`#r${i}`);
  });
  ddbWrite(
    "update-item",
    {
      TableName: table,
      Key: key,
      UpdateExpression: `REMOVE ${removes.join(", ")}`,
      ExpressionAttributeNames: names,
    },
    region
  );
}

/**
 * Crea los recursos AWS físicos (ApiKey + UsagePlan + UsagePlanKey) — la
 * parte que `register` y `rotate` comparten. No toca DynamoDB; el caller
 * decide qué escribir ahí (fila nueva en `register`, reemplazo en `rotate`).
 * El usage plan nace con el throttle de lectura a nivel plan y el de
 * escritura como override por método (ver scripts/_app-limits.mjs).
 */
function createApiKeyAndPlan({ name, envLabel, apiId, stage, limits, region }) {
  const label = `idp-${name}-${envLabel}`;

  const keyOut = JSON.parse(run(`apigateway create-api-key --name "${label}" --enabled --output json`, region));
  const apiKeyId = keyOut.id;
  const apiKeyValue = run(
    `apigateway get-api-key --api-key ${apiKeyId} --include-value --query value --output text`,
    region
  );

  const planOut = JSON.parse(
    cliJson(
      "apigateway create-usage-plan",
      usagePlanInput({ name: label, apiId, stage, write: limits.write, read: limits.read, quotaPerDay: limits.quotaPerDay }),
      region
    )
  );
  const usagePlanId = planOut.id;

  run(`apigateway create-usage-plan-key --usage-plan-id ${usagePlanId} --key-id ${apiKeyId} --key-type API_KEY --output json`, region);

  return { apiKeyId, apiKeyValue, usagePlanId };
}

/** Una línea legible con los cuatro límites de una app. */
function describeLimits({ write, read, docsPerDay, quotaPerDay }) {
  return (
    `escritura ${formatThrottle(write)} · GET ${formatThrottle(read)} · ` +
    `docs/día ${docsPerDay ?? "sin tope"} · techo requests/día ${quotaPerDay ?? "-"}`
  );
}

/** Atributos DynamoDB de los límites (copia informativa, salvo MaxDocsPerDay que SÍ lee la Lambda). */
function limitAttrs({ write, read, docsPerDay, quotaPerDay }) {
  return {
    RateLimit: { N: String(write.rateLimit) },
    BurstLimit: { N: String(write.burstLimit) },
    ReadRateLimit: { N: String(read.rateLimit) },
    ReadBurstLimit: { N: String(read.burstLimit) },
    QuotaPerDay: { N: String(quotaPerDay) },
    ...(docsPerDay != null ? { MaxDocsPerDay: { N: String(docsPerDay) } } : {}),
  };
}

/** Aviso (no error): con escritura > lectura, un POST podría quedar limitado por el throttle del plan. */
function warnIfWriteAboveRead({ write, read }) {
  if (write.rateLimit > read.rateLimit || write.burstLimit > read.burstLimit) {
    console.warn(
      `⚠️  El throttle de escritura (${formatThrottle(write)}) es mayor que el de lectura (${formatThrottle(read)}). ` +
        "Lo normal es escritura ≤ lectura; revisa que sea intencional."
    );
  }
}

function printNewKey({ name, apiKeyId, apiKeyValue, usagePlanId, limits, region }) {
  console.log(`   ApiKeyId:    ${apiKeyId}`);
  console.log(`   UsagePlanId: ${usagePlanId}`);
  console.log(`   Límites:     ${describeLimits(limits)}`);
  console.log(`\n   🔑 Valor de la key (se puede reobtener cuando sea con):`);
  console.log(`      aws apigateway get-api-key --api-key ${apiKeyId} --include-value --query value --output text --region ${region}\n`);
  console.log(`   ${apiKeyValue}`);
  console.log(`\n   Ejemplo:`);
  console.log(`      curl -H "x-api-key: ${apiKeyValue}" <endpoint>/documents/...`);
  console.log(`\n   Entrégale este valor a '${name}' — no queda en ningún log de este script ni en DynamoDB (solo el ApiKeyId).`);
}

/**
 * Valida los flags de límites. Cada campo es el número pedido o `null` si el
 * flag no vino; `removeDocsPerDay` = `--docs-per-day none`.
 */
function parseLimitFlags(args) {
  const spec = [
    ["rate-limit", "rateLimit", false],
    ["burst-limit", "burstLimit", true],
    ["read-rate-limit", "readRateLimit", false],
    ["read-burst-limit", "readBurstLimit", true],
    ["quota-per-day", "quotaPerDay", true],
  ];
  const out = { docsPerDay: null, removeDocsPerDay: false };
  for (const [flag, key, integer] of spec) {
    out[key] = null;
    if (args[flag] === undefined) continue;
    const parsed = parseLimit(args[flag], `--${flag}`, { integer });
    if (parsed.error) fail(parsed.error);
    out[key] = parsed.value;
  }
  if (args["docs-per-day"] !== undefined) {
    if (String(args["docs-per-day"]).trim().toLowerCase() === "none") {
      out.removeDocsPerDay = true;
    } else {
      const parsed = parseLimit(args["docs-per-day"], "--docs-per-day", { integer: true });
      if (parsed.error) fail(parsed.error);
      out.docsPerDay = parsed.value;
    }
  }
  return out;
}

/**
 * Límites completos para `register`: flags → si no, los mismos defaults de
 * siempre (20/40, cuota 5000), lectura 4× la escritura y sin tope de
 * documentos (retrocompatible para scripting; el modo interactivo sí propone uno).
 */
function resolveRegisterLimits(args) {
  const lim = parseLimitFlags(args);
  const write = { rateLimit: lim.rateLimit ?? 20, burstLimit: lim.burstLimit ?? 40 };
  const rec = recommendReadLimits(write);
  return {
    write,
    read: {
      rateLimit: lim.readRateLimit ?? rec.readRateLimit,
      burstLimit: lim.readBurstLimit ?? rec.readBurstLimit,
    },
    docsPerDay: lim.removeDocsPerDay ? null : lim.docsPerDay,
    quotaPerDay: lim.quotaPerDay ?? 5000,
  };
}

/**
 * Pregunta solo lo que falte en `args` (nada si ya viene todo por flags) y
 * SIEMPRE pide confirmación final antes de tocar AWS — incluso si no hubo
 * que preguntar nada más, porque esto crea una key/usage plan reales. No
 * hace nada si no hay TTY (pipe/CI) o si se pasó --yes — en esos casos
 * cmdRegister valida y falla igual que antes de que esto existiera.
 */
async function promptRegister(args) {
  if (!process.stdin.isTTY || args.yes) return args;

  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    if (!args.name) args.name = await promptName(rl, "Nombre de la app (ej. CEDIS, ERP, Portal)");
    if (!args.env) args.env = await promptEnv(rl);

    const limitFlags = ["rate-limit", "burst-limit", "read-rate-limit", "read-burst-limit", "docs-per-day", "quota-per-day"];
    if (limitFlags.some((f) => args[f] === undefined)) {
      console.log("\nPara recomendar límites, cuéntame el volumen esperado.");
      const volAns = (
        await rl.question("¿Cuántos documentos por día esperas que suba esta app aproximadamente? (ej. 50, 500, 3000): ")
      ).trim();
      const rec = recommendLimits(Number(volAns) || 0);
      console.log(
        `\nRecomendado (categoría: ${rec.tierLabel}):` +
          `\n  escritura ${rec.rateLimit}/s (burst ${rec.burstLimit}) — POST /documents, reprocess, PATCH metadata` +
          `\n  GET       ${rec.readRateLimit}/s (burst ${rec.readBurstLimit}) — 4× escritura: polling, /file, búsquedas` +
          `\n  docs/día  ${rec.docsPerDay} — lo cuenta la Lambda, solo documentos nuevos/reprocesos` +
          `\n  techo requests/día ${rec.quotaPerDay} — cuota de API Gateway, cuenta TODO incluido el polling` +
          "\nEnter acepta el valor entre [corchetes], o escribe el tuyo.\n"
      );
      const ask = async (flag, label, def) => {
        if (args[flag] !== undefined) return;
        args[flag] = (await rl.question(`${label} [${def}]: `)).trim() || String(def);
      };
      await ask("rate-limit", "Escritura: rate limit, req/s", rec.rateLimit);
      await ask("burst-limit", "Escritura: burst", rec.burstLimit);
      // Si el usuario cambió la escritura, la recomendación de lectura la sigue (4×).
      const readRec = recommendReadLimits({
        rateLimit: Number(args["rate-limit"]) || rec.rateLimit,
        burstLimit: Number(args["burst-limit"]) || rec.burstLimit,
      });
      await ask("read-rate-limit", "GET: rate limit, req/s", readRec.readRateLimit);
      await ask("read-burst-limit", "GET: burst", readRec.readBurstLimit);
      await ask("docs-per-day", "Documentos por día (`none` = sin tope)", rec.docsPerDay);
      await ask("quota-per-day", "Techo de requests por día", rec.quotaPerDay);
    }

    const limits = resolveRegisterLimits(args);
    console.log(`\nResumen: app='${args.name}' env=${args.env} → ${describeLimits(limits)}`);
    await confirmOrExit(rl, "¿Crear esta app?");
  } finally {
    rl.close();
  }
  return args;
}

async function cmdRegister(args) {
  args = await promptRegister(args);
  if (!args.name) fail("Falta --name <appName>. Uso: pnpm apps:register -- --name <appName> --env dev|prod");
  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const stack = resolveStack(args);
  const table = resolveAppsTable(args);
  const envLabel = args.env === "prod" ? "prod" : "dev";
  // Validar TODO (límites y --access-scope) antes de tocar AWS — si esto
  // corriera después de crear la key/usage plan, un flag inválido dejaría
  // recursos huérfanos en AWS sin fila en DynamoDB que los referencie.
  const limits = resolveRegisterLimits(args);
  const perms = parsePermissionFlags(args);
  warnIfWriteAboveRead(limits);

  console.log(`📋 Registrando app '${args.name}' (${envLabel}) contra ${stack}...`);

  const existing = findAppByName(table, args.name, region, { activeOnly: true });
  if (existing && !args.force) {
    fail(
      `Ya existe una app ACTIVE llamada '${args.name}' (ApiKeyId ${existing.ApiKeyId}). ` +
        `Si es para reemplazarla usa 'pnpm apps:rotate -- --name ${args.name} --env ${envLabel}' ` +
        `(evita quedar con dos keys ACTIVE del mismo nombre, que 'revoke'/'rotate' no podrían desambiguar). ` +
        `--force sí crea una key adicional a propósito (ej. dos credenciales para la misma app en paralelo).`
    );
  }

  const apiId = resolveRestApiId(stack, region);
  const stage = resolveStage(apiId, region);
  const { apiKeyId, apiKeyValue, usagePlanId } = createApiKeyAndPlan({
    name: args.name, envLabel, apiId, stage, limits, region,
  });

  // Secure by default: AccessScope="own" (solo ve lo que ella subió) y
  // AllowFieldSearch=false y AllowMetadataWrite=false salvo que se pidan
  // explícitamente — ver documentacion/AMBIENTES_Y_SEGURIDAD.md.
  ddbPutItem(
    table,
    {
      ApiKeyId: { S: apiKeyId },
      AppName: { S: args.name },
      UsagePlanId: { S: usagePlanId },
      Status: { S: "ACTIVE" },
      ...limitAttrs(limits),
      Environment: { S: envLabel },
      CreatedAt: { S: new Date().toISOString() },
      AccessScope: perms.setAttrs.AccessScope || { S: "own" },
      AllowFieldSearch: perms.setAttrs.AllowFieldSearch || { BOOL: false },
      AllowMetadataWrite: perms.setAttrs.AllowMetadataWrite || { BOOL: false },
      ...(perms.setAttrs.AllowedDocTypes ? { AllowedDocTypes: perms.setAttrs.AllowedDocTypes } : {}),
    },
    region
  );

  console.log(`\n✅ App '${args.name}' registrada.`);
  printNewKey({ name: args.name, apiKeyId, apiKeyValue, usagePlanId, limits, region });
  const scope = perms.setAttrs.AccessScope?.S || "own";
  console.log(`   Permisos: accessScope=${scope}, allowFieldSearch=${perms.setAttrs.AllowFieldSearch?.BOOL ?? false}, ` +
    `allowMetadataWrite=${perms.setAttrs.AllowMetadataWrite?.BOOL ?? false}` +
    (perms.setAttrs.AllowedDocTypes ? `, allowedDocTypes=${perms.setAttrs.AllowedDocTypes.SS.join(",")}` : ""));
}

/** Pregunta nombre/ambiente si faltan y SIEMPRE confirma antes de rotar (crea key nueva + deshabilita la vieja). */
async function promptRotate(args) {
  if (!process.stdin.isTTY || args.yes) return args;

  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    if (!args.name) args.name = await promptName(rl, "Nombre de la app a rotar");
    if (!args.env) args.env = await promptEnv(rl);
    console.log(`\nEsto crea una key NUEVA para '${args.name}' y deshabilita (no borra) la actual.`);
    await confirmOrExit(rl, "¿Rotar esta app?");
  } finally {
    rl.close();
  }
  return args;
}

async function cmdRotate(args) {
  args = await promptRotate(args);
  if (!args.name) fail("Falta --name <appName>. Uso: pnpm apps:rotate -- --name <appName> --env dev|prod");
  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const stack = resolveStack(args);
  const table = resolveAppsTable(args);
  const envLabel = args.env === "prod" ? "prod" : "dev";

  const old = findAppByName(table, args.name, region, { activeOnly: true });
  if (!old) {
    fail(
      `No hay ninguna app ACTIVE llamada '${args.name}' en ${table} para rotar. ` +
        `Usa 'pnpm apps:register -- --name ${args.name} --env ${envLabel}' si es la primera vez.`
    );
  }

  // Mismos límites que la key vieja salvo que se pidan otros explícitamente —
  // rotar no debería cambiar el rate limit de la app por accidente. Una app
  // de antes de separar lectura/escritura no tiene ReadRateLimit: su plan
  // viejo aplicaba el mismo throttle a todo, así que se conserva tal cual.
  const lim = parseLimitFlags(args);
  const write = {
    rateLimit: lim.rateLimit ?? old.RateLimit ?? 20,
    burstLimit: lim.burstLimit ?? old.BurstLimit ?? 40,
  };
  const limits = {
    write,
    read: {
      rateLimit: lim.readRateLimit ?? old.ReadRateLimit ?? write.rateLimit,
      burstLimit: lim.readBurstLimit ?? old.ReadBurstLimit ?? write.burstLimit,
    },
    docsPerDay: lim.removeDocsPerDay ? null : lim.docsPerDay ?? old.MaxDocsPerDay ?? null,
    quotaPerDay: lim.quotaPerDay ?? old.QuotaPerDay ?? 5000,
  };
  warnIfWriteAboveRead(limits);

  console.log(`🔄 Rotando '${args.name}' (${envLabel}) — key vieja ${old.ApiKeyId} quedará deshabilitada, no borrada.`);

  const apiId = resolveRestApiId(stack, region);
  const stage = resolveStage(apiId, region);
  const { apiKeyId, apiKeyValue, usagePlanId } = createApiKeyAndPlan({
    name: args.name, envLabel, apiId, stage, limits, region,
  });

  // Nueva fila primero (si algo falla después, al menos la key nueva ya
  // quedó registrada y localizable); luego se apaga la vieja.
  ddbPutItem(
    table,
    {
      ApiKeyId: { S: apiKeyId },
      AppName: { S: args.name },
      UsagePlanId: { S: usagePlanId },
      Status: { S: "ACTIVE" },
      ...limitAttrs(limits),
      Environment: { S: envLabel },
      CreatedAt: { S: new Date().toISOString() },
      RotatedFrom: { S: old.ApiKeyId },
      AccessScope: { S: old.AccessScope || "own" },
      AllowFieldSearch: { BOOL: old.AllowFieldSearch ?? false },
      AllowMetadataWrite: { BOOL: old.AllowMetadataWrite ?? false },
      ...(old.AllowedDocTypes ? { AllowedDocTypes: { SS: old.AllowedDocTypes } } : {}),
    },
    region
  );

  run(`apigateway update-api-key --api-key ${old.ApiKeyId} --patch-operations op=replace,path=/enabled,value=false --output json`, region);
  ddbUpdateItem(
    table,
    { ApiKeyId: { S: old.ApiKeyId } },
    { Status: { S: "REVOKED" }, RevokedAt: { S: new Date().toISOString() }, RotatedTo: { S: apiKeyId } },
    region
  );

  console.log(`\n✅ '${args.name}' rotada. Key vieja (${old.ApiKeyId}) deshabilitada; consumidores deben actualizar a la nueva.`);
  console.log(`   El contador de documentos/día empieza de cero con la key nueva (va por ApiKeyId).`);
  printNewKey({ name: args.name, apiKeyId, apiKeyValue, usagePlanId, limits, region });
}

/** `get-usage-plan` → límites vigentes (ver effectiveLimits en _app-limits.mjs). */
function getUsagePlan(usagePlanId, region, opts = {}) {
  const out = run(`apigateway get-usage-plan --usage-plan-id ${usagePlanId} --output json`, region, opts);
  return out ? JSON.parse(out) : null;
}

/**
 * Igual que promptRegister, pero para reconfigure: solo pregunta lo que
 * falte, y para los límites usa el valor ACTUAL de la app (no una
 * recomendación desde cero — aquí se está editando algo que ya existe) como
 * default de cada pregunta. Enter en una pregunta de límite = "no tocar ese
 * campo", no "usar el valor actual explícitamente" (deja el patch parcial
 * intacto). Hace el lookup de la app aquí mismo para poder mostrar los
 * valores actuales, y lo devuelve para que cmdReconfigure no repita el Scan.
 */
async function promptReconfigure(args) {
  if (!process.stdin.isTTY || args.yes) return { args, app: null };

  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    if (!args.name) args.name = await promptName(rl, "Nombre de la app a reconfigurar");
    if (!args.env) args.env = await promptEnv(rl);

    const region = args.region || process.env.AWS_REGION || "us-east-1";
    const table = resolveAppsTable(args);
    const app = findAppByName(table, args.name, region, { activeOnly: true });
    if (!app) {
      fail(
        `No hay ninguna app ACTIVE llamada '${args.name}' en ${table}. ` +
          `Usa 'pnpm apps:list -- --table ${table}' para ver qué apps existen.`
      );
    }

    const limitFlags = ["rate-limit", "burst-limit", "read-rate-limit", "read-burst-limit", "docs-per-day", "quota-per-day"];
    // Solo preguntar límites si no vino NINGÚN flag de cambio: quien pasó
    // --allow-field-search no quiere que se le pregunte por el rate limit.
    const anyChangeFlag = limitFlags.some((f) => args[f] !== undefined) ||
      Object.keys(parsePermissionFlags(args).setAttrs).length > 0 ||
      args["allowed-doctypes"] !== undefined;
    let current = null;
    if (!anyChangeFlag) {
      const plan = app.UsagePlanId ? getUsagePlan(app.UsagePlanId, region, { silent: true }) : null;
      current = plan ? effectiveLimits(plan) : null;
      console.log(
        `\nValores actuales de '${args.name}' (leídos del usage plan real): ` +
          (current
            ? describeLimits({ ...current, docsPerDay: app.MaxDocsPerDay })
            : `sin usage plan legible — docs/día ${app.MaxDocsPerDay ?? "sin tope"}`)
      );
      if (current?.legacy) {
        console.log("   (plan antiguo: GET y escrituras comparten el mismo throttle — fijar uno de los dos los separa)");
      }
      console.log("Enter deja ese campo sin cambios; escribe un valor para reemplazarlo.\n");
      const ask = async (flag, label) => {
        const ans = (await rl.question(`${label} [sin cambio]: `)).trim();
        if (ans) args[flag] = ans;
      };
      await ask("rate-limit", "Escritura: rate limit nuevo, req/s");
      await ask("burst-limit", "Escritura: burst nuevo");
      const writeRate = Number(args["rate-limit"] ?? current?.write.rateLimit);
      const writeBurst = Number(args["burst-limit"] ?? current?.write.burstLimit);
      const readRec = Number.isFinite(writeRate) && Number.isFinite(writeBurst)
        ? recommendReadLimits({ rateLimit: writeRate, burstLimit: writeBurst })
        : null;
      await ask(
        "read-rate-limit",
        `GET: rate limit nuevo, req/s${readRec ? ` (recomendado 4× escritura = ${readRec.readRateLimit})` : ""}`
      );
      await ask(
        "read-burst-limit",
        `GET: burst nuevo${readRec ? ` (recomendado ${readRec.readBurstLimit})` : ""}`
      );
      await ask("docs-per-day", "Documentos por día nuevo (`none` = quitar tope)");
      await ask("quota-per-day", "Techo de requests por día nuevo");
    }

    const lim = parseLimitFlags(args);
    const changes = [
      lim.rateLimit != null && `escritura rate=${lim.rateLimit}/s`,
      lim.burstLimit != null && `escritura burst=${lim.burstLimit}`,
      lim.readRateLimit != null && `GET rate=${lim.readRateLimit}/s`,
      lim.readBurstLimit != null && `GET burst=${lim.readBurstLimit}`,
      lim.docsPerDay != null && `docs/día=${lim.docsPerDay}`,
      lim.removeDocsPerDay && "docs/día=sin tope",
      lim.quotaPerDay != null && `techo requests/día=${lim.quotaPerDay}`,
    ].filter(Boolean);
    console.log(`\nResumen: '${args.name}' (${args.env}) → ${changes.length ? changes.join(", ") : "solo permisos"}`);
    await confirmOrExit(rl, "¿Aplicar estos cambios?");

    return { args, app };
  } finally {
    rl.close();
  }
}

async function cmdReconfigure(args) {
  const prompted = await promptReconfigure(args);
  args = prompted.args;

  if (!args.name) {
    fail(
      "Falta --name <appName>. Uso: pnpm apps:reconfigure -- --name <appName> [--rate-limit N] [--burst-limit N] " +
        "[--read-rate-limit N] [--read-burst-limit N] [--docs-per-day N|none] [--quota-per-day N]"
    );
  }
  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const table = resolveAppsTable(args);

  const lim = parseLimitFlags(args);
  const perms = parsePermissionFlags(args);
  const hasPermChanges = Object.keys(perms.setAttrs).length > 0 || perms.removeAttrs.length > 0;
  const wantsThrottle = [lim.rateLimit, lim.burstLimit, lim.readRateLimit, lim.readBurstLimit].some((v) => v !== null);
  const wantsQuota = lim.quotaPerDay !== null;
  const wantsDocs = lim.docsPerDay !== null || lim.removeDocsPerDay;
  if (!wantsThrottle && !wantsQuota && !wantsDocs && !hasPermChanges) {
    fail(
      "Falta al menos uno de --rate-limit/--burst-limit/--read-rate-limit/--read-burst-limit/" +
        "--docs-per-day/--quota-per-day/--access-scope/--allowed-doctypes/--allow-field-search/" +
        "--allow-metadata-write (esto solo cambia configuración, no crea ni revoca nada — usa 'rotate' para eso)."
    );
  }

  const app = prompted.app || findAppByName(table, args.name, region, { activeOnly: true });
  if (!app) {
    fail(
      `No hay ninguna app ACTIVE llamada '${args.name}' en ${table}. ` +
        `Usa 'pnpm apps:list -- --table ${table}' para ver qué apps existen.`
    );
  }

  // Throttle y cuota viven en el usage plan de API Gateway y necesitan
  // UsagePlanId; docs/día y permisos son solo atributos en DynamoDB — no
  // dependen de él, así que una app adoptada sin UsagePlanId conocido igual
  // puede reconfigurarlos, solo no su throttle/cuota.
  if ((wantsThrottle || wantsQuota) && !app.UsagePlanId) {
    fail(
      `'${args.name}' no tiene UsagePlanId registrado (probablemente adoptada sin uno con 'apps:adopt') — ` +
        `ajusta su usage plan a mano en la consola/CLI, este comando no puede inferir cuál es.`
    );
  }

  const updates = { UpdatedAt: { S: new Date().toISOString() }, ...perms.setAttrs };
  const removeAttrs = [...perms.removeAttrs];
  let applied = null;

  if (wantsThrottle || wantsQuota) {
    // Se parte de lo que el usage plan tiene DE VERDAD (no de la copia en
    // DynamoDB) y se completa lo no pedido con eso: un override nuevo
    // necesita rate y burst juntos. En un plan antiguo (sin overrides) el
    // throttle del plan hacía de escritura y de lectura a la vez, así que
    // fijar cualquiera de los dos crea los overrides de escritura con los
    // valores de hoy — nada se afloja ni se aprieta sin pedirlo.
    const plan = getUsagePlan(app.UsagePlanId, region);
    const current = effectiveLimits(plan);
    let read = null;
    let write = null;
    if (wantsThrottle) {
      write = {
        rateLimit: lim.rateLimit ?? current.write.rateLimit,
        burstLimit: lim.burstLimit ?? current.write.burstLimit,
      };
      read = {
        rateLimit: lim.readRateLimit ?? current.read.rateLimit,
        burstLimit: lim.readBurstLimit ?? current.read.burstLimit,
      };
      warnIfWriteAboveRead({ write, read });
    }
    const patchOperations = usagePlanPatchOps(plan, { read, write, quotaPerDay: lim.quotaPerDay });
    console.log(`⚙️  Reconfigurando límites de '${args.name}' (UsagePlanId ${app.UsagePlanId})...`);
    cliJson("apigateway update-usage-plan", { usagePlanId: app.UsagePlanId, patchOperations }, region);

    applied = { write: write ?? current.write, read: read ?? current.read, quotaPerDay: lim.quotaPerDay ?? current.quotaPerDay };
    if (write) {
      updates.RateLimit = { N: String(write.rateLimit) };
      updates.BurstLimit = { N: String(write.burstLimit) };
      updates.ReadRateLimit = { N: String(read.rateLimit) };
      updates.ReadBurstLimit = { N: String(read.burstLimit) };
    }
    if (wantsQuota) updates.QuotaPerDay = { N: String(lim.quotaPerDay) };
  }

  if (lim.docsPerDay !== null) updates.MaxDocsPerDay = { N: String(lim.docsPerDay) };
  if (lim.removeDocsPerDay) removeAttrs.push("MaxDocsPerDay");

  ddbUpdateItem(table, { ApiKeyId: { S: app.ApiKeyId } }, updates, region);
  if (removeAttrs.length) {
    ddbRemoveAttrs(table, { ApiKeyId: { S: app.ApiKeyId } }, removeAttrs, region);
  }

  console.log(`\n✅ '${args.name}' reconfigurada. La API key NO cambió (${app.ApiKeyId}) — los consumidores no necesitan actualizar nada.`);
  if (applied || wantsDocs) {
    const docsPerDay = lim.removeDocsPerDay ? null : lim.docsPerDay ?? app.MaxDocsPerDay ?? null;
    console.log(
      applied
        ? `   ${describeLimits({ ...applied, docsPerDay })}`
        : `   docs/día ${docsPerDay ?? "sin tope"}`
    );
  }
  if (hasPermChanges) {
    console.log(
      `   accessScope=${perms.setAttrs.AccessScope?.S ?? app.AccessScope ?? "own"} ` +
        `allowFieldSearch=${perms.setAttrs.AllowFieldSearch?.BOOL ?? app.AllowFieldSearch ?? false}` +
        ` allowMetadataWrite=${perms.setAttrs.AllowMetadataWrite?.BOOL ?? app.AllowMetadataWrite ?? false}` +
        (perms.setAttrs.AllowedDocTypes ? ` allowedDocTypes=${perms.setAttrs.AllowedDocTypes.SS.join(",")}` : "") +
        (perms.removeAttrs.includes("AllowedDocTypes") ? " allowedDocTypes=(quitado, ve todos los tipos permitidos por su scope)" : "")
    );
  }
}

/**
 * `AllowedDocTypes` unión/diferencia contra lo que la app YA tiene — a
 * diferencia de `reconfigure --allowed-doctypes`, que REEMPLAZA la lista
 * entera (hay que retipear los tipos existentes para agregar uno más), esto
 * agrega/quita SOLO los tipos pedidos. Devuelve `{ setAttr, removeAttr }`:
 * `removeAttr=true` cuando la resta deja la lista vacía — DynamoDB no permite
 * un String Set vacío, así que "sin tipos restantes" es un REMOVE del
 * atributo completo (mismo criterio que `--allowed-doctypes none`), lo que
 * en la práctica significa "sin restricción de tipo", no "no puede ver nada".
 */
function computeDocTypes(current, requestedRaw, mode) {
  const requested = String(requestedRaw)
    .split(",")
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
  const set = new Set(current || []);
  if (mode === "add") requested.forEach((t) => set.add(t));
  else requested.forEach((t) => set.delete(t));
  const result = [...set];
  return result.length ? { setAttr: { SS: result } } : { removeAttr: true };
}

/** Pregunta nombre/ambiente si faltan, busca la app, y confirma mostrando ANTES/DESPUÉS. Compartido por grant/deny. */
async function promptGrantDeny(args, verb) {
  if (!process.stdin.isTTY || args.yes) return { args, app: null };

  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    if (!args.name) args.name = await promptName(rl, `Nombre de la app a la que ${verb === "grant" ? "otorgar" : "quitar"} permisos`);
    if (!args.env) args.env = await promptEnv(rl);

    const region = args.region || process.env.AWS_REGION || "us-east-1";
    const table = resolveAppsTable(args);
    const app = findAppByName(table, args.name, region, { activeOnly: true });
    if (!app) {
      fail(
        `No hay ninguna app ACTIVE llamada '${args.name}' en ${table}. ` +
          `Usa 'pnpm apps:list -- --table ${table}' para ver qué apps existen.`
      );
    }
    console.log(
      `\nPermisos actuales de '${args.name}': accessScope=${app.AccessScope ?? "own"} ` +
        `allowFieldSearch=${app.AllowFieldSearch ?? false}` +
        ` allowMetadataWrite=${app.AllowMetadataWrite ?? false}` +
        (app.AllowedDocTypes ? ` allowedDocTypes=${app.AllowedDocTypes.join(",")}` : "")
    );
    return { args, app };
  } finally {
    rl.close();
  }
}

/** Confirma con el resumen de qué va a cambiar — mismo mensaje para grant y deny, el propio caller arma `notes`. */
async function confirmGrantDeny(args, verb, notes) {
  if (!process.stdin.isTTY || args.yes) return;
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    console.log(`${verb === "grant" ? "Otorgar" : "Quitar"} a '${args.name}': ${notes.join(", ")}`);
    await confirmOrExit(rl, "¿Aplicar?");
  } finally {
    rl.close();
  }
}

async function cmdGrant(args) {
  const prompted = await promptGrantDeny(args, "grant");
  args = prompted.args;
  if (!args.name) fail("Falta --name <appName>. Uso: pnpm apps:grant -- --name <appName> [--doctype A,B] [--access-scope all] [--field-search] [--metadata-write]");

  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const table = resolveAppsTable(args);
  const app = prompted.app || findAppByName(table, args.name, region, { activeOnly: true });
  if (!app) fail(`No hay ninguna app ACTIVE llamada '${args.name}' en ${table}.`);

  const updates = { UpdatedAt: { S: new Date().toISOString() } };
  const notes = [];

  if (args["access-scope"] !== undefined) {
    const v = String(args["access-scope"]).trim().toLowerCase();
    if (v !== "own" && v !== "all") fail("--access-scope debe ser 'own' o 'all'.");
    updates.AccessScope = { S: v };
    notes.push(`accessScope=${v}`);
  }
  if (args.doctype !== undefined) {
    const { setAttr } = computeDocTypes(app.AllowedDocTypes, args.doctype, "add");
    if (setAttr) updates.AllowedDocTypes = setAttr; // "add" nunca vacía la lista, siempre hay setAttr
    notes.push(`+doctype(s)=${args.doctype}`);
  }
  if (args["field-search"]) {
    updates.AllowFieldSearch = { BOOL: true };
    notes.push("allowFieldSearch=true");
  }
  if (args["metadata-write"]) {
    updates.AllowMetadataWrite = { BOOL: true };
    notes.push("allowMetadataWrite=true");
  }
  if (!notes.length) {
    fail("Nada que otorgar — pasa --doctype A,B, --access-scope all, --field-search y/o --metadata-write.");
  }

  await confirmGrantDeny(args, "grant", notes);
  ddbUpdateItem(table, { ApiKeyId: { S: app.ApiKeyId } }, updates, region);
  console.log(`\n✅ Otorgado a '${args.name}': ${notes.join(", ")}.`);
}

async function cmdDeny(args) {
  const prompted = await promptGrantDeny(args, "deny");
  args = prompted.args;
  if (!args.name) fail("Falta --name <appName>. Uso: pnpm apps:deny -- --name <appName> [--doctype A,B] [--access-scope own] [--field-search] [--metadata-write]");

  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const table = resolveAppsTable(args);
  const app = prompted.app || findAppByName(table, args.name, region, { activeOnly: true });
  if (!app) fail(`No hay ninguna app ACTIVE llamada '${args.name}' en ${table}.`);

  const updates = { UpdatedAt: { S: new Date().toISOString() } };
  const removeAttrs = [];
  const notes = [];

  if (args["access-scope"] !== undefined) {
    const v = String(args["access-scope"]).trim().toLowerCase();
    if (v !== "own" && v !== "all") fail("--access-scope debe ser 'own' o 'all'.");
    updates.AccessScope = { S: v };
    notes.push(`accessScope=${v}`);
  }
  if (args.doctype !== undefined) {
    const { setAttr, removeAttr } = computeDocTypes(app.AllowedDocTypes, args.doctype, "remove");
    if (removeAttr) removeAttrs.push("AllowedDocTypes");
    else updates.AllowedDocTypes = setAttr;
    notes.push(`-doctype(s)=${args.doctype}`);
  }
  // Nombrado --field-search (no --disallow-field-search) por simetría con
  // `deny`: aquí "field-search" siempre significa "quitar ese permiso".
  if (args["field-search"]) {
    updates.AllowFieldSearch = { BOOL: false };
    notes.push("allowFieldSearch=false");
  }
  if (args["metadata-write"]) {
    updates.AllowMetadataWrite = { BOOL: false };
    notes.push("allowMetadataWrite=false");
  }
  if (!notes.length) {
    fail("Nada que quitar — pasa --doctype A,B, --access-scope own, --field-search y/o --metadata-write.");
  }

  await confirmGrantDeny(args, "deny", notes);
  ddbUpdateItem(table, { ApiKeyId: { S: app.ApiKeyId } }, updates, region);
  if (removeAttrs.length) ddbRemoveAttrs(table, { ApiKeyId: { S: app.ApiKeyId } }, removeAttrs, region);
  console.log(`\n✅ Quitado a '${args.name}': ${notes.join(", ")}.`);
}

function cmdList(args) {
  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const table = resolveAppsTable(args);
  const items = scanApps(table, region).sort((a, b) => (a.AppName || "").localeCompare(b.AppName || ""));

  if (!items.length) {
    console.log(`(sin apps registradas en ${table})`);
    return;
  }

  console.log(`📋 Apps en ${table}:\n`);
  for (const it of items) {
    const icon = it.Status === "ACTIVE" ? "✅" : "🚫";
    console.log(`  ${icon} ${it.AppName}  [${it.Status}]`);
    console.log(`     ApiKeyId: ${it.ApiKeyId}  UsagePlanId: ${it.UsagePlanId || "-"}  creada ${it.CreatedAt || "-"}`);
    // Copia registrada en DynamoDB; los valores vigentes de verdad los muestra `apps:usage`.
    // Sin ReadRateLimit = app de antes de separar lectura/escritura (mismo throttle para todo).
    const write = { rateLimit: it.RateLimit, burstLimit: it.BurstLimit };
    const read = it.ReadRateLimit != null ? { rateLimit: it.ReadRateLimit, burstLimit: it.ReadBurstLimit } : write;
    console.log(`     ${describeLimits({ write, read, docsPerDay: it.MaxDocsPerDay, quotaPerDay: it.QuotaPerDay })}`);
    console.log(
      `     accessScope=${it.AccessScope ?? "own"} allowFieldSearch=${it.AllowFieldSearch ?? false}` +
        ` allowMetadataWrite=${it.AllowMetadataWrite ?? false}` +
        (it.AllowedDocTypes ? ` allowedDocTypes=${it.AllowedDocTypes.join(",")}` : "")
    );
    // RevokedAt no se borra al hacer `enable` (queda como historial de
    // cuándo se revocó la última vez) — solo mostrarlo como "revocada" si
    // sigue siendo el estado actual, para no confundir una app ACTIVE que
    // ya fue reactivada con una que sigue deshabilitada.
    if (it.RevokedAt && it.Status !== "ACTIVE") console.log(`     revocada ${it.RevokedAt}`);
    console.log("");
  }
}

/** update-api-key(enabled=false) + Status=REVOKED — usada por `revoke` y `disable` (revoke = esto + --delete opcional). */
function disableApiKey(table, app, region) {
  run(`apigateway update-api-key --api-key ${app.ApiKeyId} --patch-operations op=replace,path=/enabled,value=false --output json`, region);
  ddbUpdateItem(
    table,
    { ApiKeyId: { S: app.ApiKeyId } },
    { Status: { S: "REVOKED" }, RevokedAt: { S: new Date().toISOString() } },
    region
  );
}

/** Pregunta nombre/ambiente si faltan y SIEMPRE confirma — con una advertencia extra si --delete (irreversible). */
async function promptRevoke(args) {
  if (!process.stdin.isTTY || args.yes) return args;

  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    if (!args.name) args.name = await promptName(rl, "Nombre de la app a revocar");
    if (!args.env) args.env = await promptEnv(rl);
    if (args.delete) {
      console.log(`\n⚠️  --delete además BORRARÁ los recursos AWS de '${args.name}' — irreversible.`);
      await confirmOrExit(rl, "¿Revocar Y BORRAR esta app?");
    } else {
      await confirmOrExit(rl, `¿Revocar (deshabilitar) '${args.name}'?`);
    }
  } finally {
    rl.close();
  }
  return args;
}

async function cmdRevoke(args) {
  args = await promptRevoke(args);
  if (!args.name) fail("Falta --name <appName>. Uso: pnpm apps:revoke -- --name <appName> --env dev|prod");
  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const table = resolveAppsTable(args);

  const app = findAppByName(table, args.name, region);
  if (!app) fail(`No encontré ninguna app llamada '${args.name}' en ${table}.`);

  if (app.Status === "REVOKED") {
    console.log(`⚠️  '${args.name}' ya estaba revocada (${app.RevokedAt || "fecha desconocida"}).`);
  } else {
    disableApiKey(table, app, region);
    console.log(`🚫 '${args.name}' revocada (key deshabilitada en API Gateway, no borrada).`);
  }

  if (args.delete) {
    console.log(`   Borrando recursos AWS (--delete, irreversible)...`);
    if (app.UsagePlanId) {
      run(`apigateway delete-usage-plan-key --usage-plan-id ${app.UsagePlanId} --key-id ${app.ApiKeyId} --output json`, region, { silent: true });
      // API Gateway rechaza borrar un usage plan que aún tiene API stages
      // asociados ("Cannot delete Usage Plan ... because there are API Stages
      // associated with it") — hay que desasociarlos primero.
      const plan = getUsagePlan(app.UsagePlanId, region, { silent: true });
      for (const apiStage of plan?.apiStages || []) {
        cliJson(
          "apigateway update-usage-plan",
          {
            usagePlanId: app.UsagePlanId,
            patchOperations: [{ op: "remove", path: "/apiStages", value: `${apiStage.apiId}:${apiStage.stage}` }],
          },
          region,
          { silent: true }
        );
      }
      const deleted = run(`apigateway delete-usage-plan --usage-plan-id ${app.UsagePlanId} --output json`, region, { silent: true });
      if (deleted === null && getUsagePlan(app.UsagePlanId, region, { silent: true })) {
        console.warn(`   ⚠️  No se pudo borrar el usage plan ${app.UsagePlanId}; bórralo a mano en la consola/CLI.`);
      }
    }
    run(`apigateway delete-api-key --api-key ${app.ApiKeyId} --output json`, region, { silent: true });
    console.log(`   Recursos AWS eliminados. El registro en ${table} se conserva como histórico (Status=REVOKED).`);
  }
}

/** Pregunta nombre/ambiente si faltan y SIEMPRE confirma antes de deshabilitar (`apps:disable` — igual que `revoke` sin --delete). */
async function promptDisable(args) {
  if (!process.stdin.isTTY || args.yes) return args;

  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    if (!args.name) args.name = await promptName(rl, "Nombre de la app a deshabilitar");
    if (!args.env) args.env = await promptEnv(rl);
    await confirmOrExit(rl, `¿Deshabilitar '${args.name}'? (la key queda inhabilitada, no se borra)`);
  } finally {
    rl.close();
  }
  return args;
}

async function cmdDisable(args) {
  args = await promptDisable(args);
  if (!args.name) fail("Falta --name <appName>. Uso: pnpm apps:disable -- --name <appName> --env dev|prod");
  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const table = resolveAppsTable(args);

  const app = findAppByName(table, args.name, region, { activeOnly: true });
  if (!app) fail(`No hay ninguna app ACTIVE llamada '${args.name}' en ${table}.`);

  disableApiKey(table, app, region);
  console.log(
    `🚫 '${args.name}' deshabilitada (key inhabilitada en API Gateway, no borrada). ` +
      `Usa 'pnpm apps:enable -- --name ${args.name} --env ${args.env}' para revertir.`
  );
}

/** Pregunta nombre/ambiente si faltan y SIEMPRE confirma antes de re-habilitar una app previamente deshabilitada. */
async function promptEnable(args) {
  if (!process.stdin.isTTY || args.yes) return args;

  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    if (!args.name) args.name = await promptName(rl, "Nombre de la app a habilitar");
    if (!args.env) args.env = await promptEnv(rl);
    await confirmOrExit(rl, `¿Habilitar de nuevo '${args.name}'?`);
  } finally {
    rl.close();
  }
  return args;
}

async function cmdEnable(args) {
  args = await promptEnable(args);
  if (!args.name) fail("Falta --name <appName>. Uso: pnpm apps:enable -- --name <appName> --env dev|prod");
  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const table = resolveAppsTable(args);

  // Sin activeOnly: justo buscamos una que NO esté ACTIVE para reactivarla.
  const app = findAppByName(table, args.name, region);
  if (!app) fail(`No encontré ninguna app llamada '${args.name}' en ${table}.`);
  if (app.Status === "ACTIVE") {
    console.log(`⚠️  '${args.name}' ya estaba ACTIVE — nada que hacer.`);
    return;
  }

  // Si la key fue borrada antes con --delete, esta llamada falla con el
  // error nativo de API Gateway (NotFoundException) — no hay nada que
  // reactivar, hay que registrar una app nueva.
  run(`apigateway update-api-key --api-key ${app.ApiKeyId} --patch-operations op=replace,path=/enabled,value=true --output json`, region);
  ddbUpdateItem(
    table,
    { ApiKeyId: { S: app.ApiKeyId } },
    { Status: { S: "ACTIVE" }, EnabledAt: { S: new Date().toISOString() } },
    region
  );

  console.log(`✅ '${args.name}' habilitada de nuevo (misma key, ${app.ApiKeyId}).`);
}

/** Pregunta nombre/api-key-id/ambiente si faltan y SIEMPRE confirma antes de escribir en DynamoDB. */
async function promptAdopt(args) {
  if (!process.stdin.isTTY || args.yes) return args;

  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    if (!args.name) args.name = await promptName(rl, "Nombre a asignarle (ej. legacy-idp-default)");
    if (!args["api-key-id"]) {
      const ans = (await rl.question("ApiKeyId a adoptar: ")).trim();
      if (!ans) fail("El ApiKeyId no puede quedar vacío.");
      args["api-key-id"] = ans;
    }
    if (!args.env) args.env = await promptEnv(rl);
    await confirmOrExit(rl, `¿Adoptar la key ${args["api-key-id"]} como '${args.name}'?`);
  } finally {
    rl.close();
  }
  return args;
}

async function cmdAdopt(args) {
  args = await promptAdopt(args);
  const apiKeyId = args["api-key-id"];
  if (!args.name || !apiKeyId) {
    fail("Uso: pnpm apps:adopt -- --name <nombre> --api-key-id <ApiKeyId> --env dev|prod");
  }
  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const table = resolveAppsTable(args);
  const envLabel = args.env === "prod" ? "prod" : "dev";

  // No crea api key ni usage plan nuevos — solo registra en la tabla Apps una
  // key/plan que ya existen (ej. la idp-default CDK-managed), para que el
  // Upload Lambda empiece a resolverle un nombre legible en vez de
  // "unregistered". Ver documentacion/AMBIENTES_Y_SEGURIDAD.md.
  let usagePlanId = "";
  let limits = null;
  const plansOut = run(`apigateway get-usage-plans --key-id ${apiKeyId} --output json`, region, { silent: true });
  if (plansOut) {
    const plans = JSON.parse(plansOut).items || [];
    if (plans.length) {
      usagePlanId = plans[0].id;
      limits = effectiveLimits(plans[0]);
    }
  }
  const knownLimitAttrs = limits
    ? Object.fromEntries(
        [
          ["RateLimit", limits.write.rateLimit],
          ["BurstLimit", limits.write.burstLimit],
          ["ReadRateLimit", limits.read.rateLimit],
          ["ReadBurstLimit", limits.read.burstLimit],
          ["QuotaPerDay", limits.quotaPerDay],
        ]
          .filter(([, v]) => v != null)
          .map(([k, v]) => [k, { N: String(v) }])
      )
    : {};
  if (!usagePlanId) {
    console.warn(`⚠️  La key ${apiKeyId} no está asociada a ningún usage plan — se adopta igual, sin límites conocidos.`);
  }

  ddbPutItem(
    table,
    {
      ApiKeyId: { S: apiKeyId },
      AppName: { S: args.name },
      ...(usagePlanId ? { UsagePlanId: { S: usagePlanId } } : {}),
      Status: { S: "ACTIVE" },
      // Sin MaxDocsPerDay: una key adoptada arranca sin tope de documentos
      // (el mismo comportamiento que tenía antes de adoptarla); fijarlo es
      // un `apps:reconfigure --docs-per-day N` explícito.
      ...knownLimitAttrs,
      Environment: { S: envLabel },
      CreatedAt: { S: new Date().toISOString() },
      AccessScope: { S: "own" },
      AllowFieldSearch: { BOOL: false },
      AllowMetadataWrite: { BOOL: false },
    },
    region
  );

  console.log(`✅ '${apiKeyId}' adoptada como '${args.name}' (${envLabel}) en ${table}.`);
}

/** YYYY-MM-DD en UTC — el mismo "día" que usan la cuota de API Gateway y daily-doc-cap.mjs. */
function utcDay(date) {
  return date.toISOString().slice(0, 10);
}

/**
 * `apps:usage` — solo lectura. Para cada app muestra los límites VIGENTES
 * leídos del usage plan real (no la copia de DynamoDB, que puede haber
 * quedado vieja si alguien tocó el plan en la consola) y, por día UTC:
 *   - requests usadas/restantes de la cuota de API Gateway (`get-usage`;
 *     cuenta TODO: POST, polling, /file, búsquedas);
 *   - documentos contados por la Lambda contra `MaxDocsPerDay`.
 * Las requests rechazadas por throttle (429) o por WAF (403) no aparecen en
 * `get-usage`: nunca llegaron a consumir cuota.
 */
function cmdUsage(args) {
  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const table = resolveAppsTable(args);
  const usageTable = resolveUsageTable(args);
  const daysParsed = parseLimit(args.days ?? 7, "--days", { integer: true });
  if (daysParsed.error) fail(daysParsed.error);
  const days = Math.min(daysParsed.value, 90);

  const end = new Date();
  const dayList = Array.from({ length: days }, (_, i) => utcDay(new Date(end.getTime() - (days - 1 - i) * 86400000)));
  const startDay = dayList[0];
  const endDay = dayList[dayList.length - 1];

  let apps = scanApps(table, region)
    .filter((a) => args.all || a.Status === "ACTIVE")
    .sort((a, b) => (a.AppName || "").localeCompare(b.AppName || ""));
  if (args.name) apps = apps.filter((a) => (a.AppName || "").toLowerCase() === String(args.name).toLowerCase());
  if (!apps.length) {
    console.log(`(sin apps ${args.all ? "" : "ACTIVE "}${args.name ? `llamadas '${args.name}' ` : ""}en ${table})`);
    return;
  }

  console.log(`📊 Uso por app en ${table} — ${startDay} → ${endDay} (días UTC)`);
  // Medido 2026-09-21: cientos de requests seguían en 0 minutos después. El
  // contador de documentos (DynamoDB) sí es inmediato.
  console.log("   Requests: `get-usage` de API Gateway NO es en tiempo real (puede tardar en reflejar lo de hoy).\n");
  let usageTableMissing = false;
  for (const app of apps) {
    const plan = app.UsagePlanId ? getUsagePlan(app.UsagePlanId, region, { silent: true }) : null;
    const lim = plan ? effectiveLimits(plan) : null;
    console.log(`  ${app.Status === "ACTIVE" ? "✅" : "🚫"} ${app.AppName}  [${app.Status}]  ApiKeyId ${app.ApiKeyId}  UsagePlan ${app.UsagePlanId || "-"}`);
    console.log(
      lim
        ? `     ${describeLimits({ ...lim, docsPerDay: app.MaxDocsPerDay })}`
        : `     (usage plan no legible) docs/día ${app.MaxDocsPerDay ?? "sin tope"}`
    );
    if (lim?.legacy) console.log("     plan antiguo: GET y escrituras comparten el mismo throttle");

    // get-usage: items[keyId] = [[usadas, restantes], ...], un par por día desde startDate.
    const requestsByDay = {};
    if (app.UsagePlanId) {
      const out = run(
        `apigateway get-usage --usage-plan-id ${app.UsagePlanId} --key-id ${app.ApiKeyId} ` +
          `--start-date ${startDay} --end-date ${endDay} --output json`,
        region,
        { silent: true }
      );
      const series = out ? JSON.parse(out).items?.[app.ApiKeyId] || [] : [];
      series.forEach(([used, remaining], i) => {
        if (dayList[i]) requestsByDay[dayList[i]] = { used, remaining };
      });
    }

    const docsByDay = {};
    const docsOut = cliJson(
      "dynamodb query",
      {
        TableName: usageTable,
        KeyConditionExpression: "ApiKeyId = :k AND #d BETWEEN :a AND :b",
        ExpressionAttributeNames: { "#d": "Day" },
        ExpressionAttributeValues: { ":k": { S: app.ApiKeyId }, ":a": { S: startDay }, ":b": { S: endDay } },
      },
      region,
      { silent: true }
    );
    if (docsOut) {
      for (const item of JSON.parse(docsOut).Items || []) {
        const it = unmarshal(item);
        docsByDay[it.Day] = it.Docs;
      }
    } else {
      usageTableMissing = true;
    }

    console.log("     Día (UTC)    Requests usadas/restantes   Docs usados/tope");
    for (const day of dayList) {
      const r = requestsByDay[day];
      // get-usage omite por completo las keys sin ninguna request en el rango.
      const reqCol = r ? `${r.used} / ${r.remaining}` : lim?.quotaPerDay != null ? `0 / ${lim.quotaPerDay}` : "-";
      const docs = docsByDay[day];
      const docsCol = `${docs ?? 0} / ${app.MaxDocsPerDay ?? "sin tope"}`;
      console.log(`     ${day}   ${reqCol.padEnd(26)}  ${docsOut ? docsCol : "-"}`);
    }
    console.log("");
  }
  if (usageTableMissing) {
    console.log(`⚠️  No pude leer ${usageTable} (¿stack sin desplegar todavía?) — la columna de documentos queda en "-".`);
  }
}

function printHelp() {
  console.log(`
Uso: pnpm apps:<comando> -- [opciones]

Comandos (todos interactivos en una terminal real si falta algo — siempre
piden confirmación S/n antes de tocar AWS, salvo --yes o sin TTY):
  register     --name X [LÍMITES] [--force]
               [--access-scope own|all] [--allowed-doctypes A,B] [--allow-field-search] [--allow-metadata-write]
  rotate       --name X [LÍMITES]                (sin flags conserva los límites de la key vieja)
  reconfigure  --name X [LÍMITES]
               [--access-scope own|all] [--allowed-doctypes A,B|none]
               [--allow-field-search] [--disallow-field-search]
               [--allow-metadata-write] [--disallow-metadata-write]
  grant        --name X [--doctype A,B] [--access-scope all] [--field-search] [--metadata-write]
               (agrega/otorga SIN pisar lo que ya tenía — ver reconfigure para reemplazar todo)
  deny         --name X [--doctype A,B] [--access-scope own] [--field-search] [--metadata-write]
               (quita SOLO lo pedido — inverso de grant)
  disable      --name X                          (deshabilita la key, no la borra)
  enable       --name X                          (reactiva una app deshabilitada)
  revoke       --name X [--delete]                (= disable, + borra recursos AWS con --delete)
  adopt        --name X --api-key-id <ApiKeyId>
  list                                           (lo registrado en DynamoDB)
  usage        [--name X] [--days 7] [--all]     (límites REALES del usage plan + consumo por día; solo lectura)

LÍMITES (register usa por default 20/40 escritura, 4× lectura, cuota 5000, sin tope de docs):
  --rate-limit N --burst-limit N             throttle de escritura (POST /documents, /upload, reprocess, PATCH metadata)
  --read-rate-limit N --read-burst-limit N   throttle de los GET (recomendado 4× escritura)
  --docs-per-day N|none                      documentos/día que cuenta la Lambda (429 DAILY_DOCUMENT_LIMIT)
  --quota-per-day N                          techo de requests/día de API Gateway (cuenta TODO, incluido el polling)

Opciones comunes: --env dev|prod (default dev), --stack <NOMBRE>, --table <NOMBRE>,
                  --usage-table <NOMBRE>, --region, --yes
`);
}

async function main() {
  const sub = process.argv[2];
  const args = parseArgs(process.argv.slice(3));

  switch (sub) {
    case "register":
      return await cmdRegister(args);
    case "rotate":
      return await cmdRotate(args);
    case "reconfigure":
      return await cmdReconfigure(args);
    case "grant":
      return await cmdGrant(args);
    case "deny":
      return await cmdDeny(args);
    case "disable":
      return await cmdDisable(args);
    case "enable":
      return await cmdEnable(args);
    case "list":
      return cmdList(args);
    case "usage":
      return cmdUsage(args);
    case "revoke":
      return await cmdRevoke(args);
    case "adopt":
      return await cmdAdopt(args);
    default:
      printHelp();
      if (sub && sub !== "--help" && sub !== "-h") process.exitCode = 1;
  }
}

main();
