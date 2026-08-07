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
 *   pnpm apps:register -- --name ERP --env prod --rate-limit 30 --burst-limit 60 --quota-per-day 10000
 *   pnpm apps:rotate -- --name CEDIS --env dev          (key nueva, la vieja queda deshabilitada)
 *   pnpm apps:reconfigure -- --name CEDIS --env dev --rate-limit 50   (solo cambia límites, misma key)
 *   pnpm apps:reconfigure -- --name CEDIS --env dev --access-scope all --allow-field-search
 *   pnpm apps:reconfigure -- --name CEDIS --env dev --allowed-doctypes FACTURA_HONORARIOS_XML
 *   pnpm apps:grant -- --name CEDIS --env dev --doctype PEDIMENTO_ADUANAL   (agrega SIN pisar lo existente)
 *   pnpm apps:deny -- --name CEDIS --env dev --field-search                (quita SOLO ese permiso)
 *   pnpm apps:disable -- --name CEDIS --env dev         (deshabilita la key, no la borra)
 *   pnpm apps:enable -- --name CEDIS --env dev          (reactiva una app deshabilitada)
 *   pnpm apps:revoke -- --name CEDIS --env dev          (= disable)
 *   pnpm apps:revoke -- --name CEDIS --env dev --delete   (+ borra los recursos AWS, irreversible)
 *   pnpm apps:adopt -- --name legacy-idp-default --api-key-id <ApiKeyId> --env prod
 *   pnpm apps:list -- --env prod
 *
 * TODOS los comandos que mutan algo (register/rotate/reconfigure/grant/deny/
 * disable/enable/revoke/adopt — `list` es de solo lectura) son interactivos cuando
 * corren en una terminal real y falta algún dato, y SIEMPRE terminan con una
 * confirmación S/n antes de tocar AWS — incluso si diste todos los flags,
 * salvo que pases `--yes` o no haya TTY (pipe/CI), en cuyo caso no preguntan
 * ni confirman nada y fallan si falta algo requerido (para scripting/CI):
 *   - `register`: si falta rate-limit/burst-limit/cuota, pide el volumen de
 *     documentos esperado por día para RECOMENDAR valores (`recommendLimits`/
 *     `VOLUME_TIERS`).
 *   - `reconfigure`: busca la app primero y muestra sus límites ACTUALES como
 *     default de cada pregunta — Enter deja ese campo sin cambios, no repite
 *     la recomendación desde cero (aquí se edita algo que ya existe).
 *   - `revoke --delete` y `rotate` muestran una advertencia extra (son las
 *     acciones más difíciles/imposibles de deshacer) antes de confirmar.
 *
 * Permisos por app (ver documentacion/AMBIENTES_Y_SEGURIDAD.md → "Acceso por
 * app" — los aplica query-lambda en cada GET /documents*). Dos formas de
 * tocarlos, según si quieres REEMPLAZAR todo o solo AJUSTAR un permiso:
 *   - `register`/`reconfigure --allowed-doctypes A,B`: fija la lista EXACTA
 *     (pisa lo que hubiera). Para quitar la restricción: `--allowed-doctypes none`.
 *   - `grant`/`deny --doctype A,B`: suma/resta esos tipos a la lista actual
 *     SIN tocar el resto — no hace falta saber/retipear los que ya tenía.
 * En ambos casos, los mismos tres permisos:
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
 * con permisos de apigateway (ApiKey / UsagePlan) y dynamodb (Scan / PutItem
 * / UpdateItem).
 */

import { execSync } from "child_process";
import { writeFileSync, unlinkSync } from "fs";
import { createInterface } from "node:readline/promises";

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
 * para quitar la restricción), --allow-field-search/--disallow-field-search.
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

function ddbWrite(op, payload, region) {
  const tmp = `/tmp/ddb-apps-${Date.now()}-${Math.random().toString(36).slice(2)}.json`;
  writeFileSync(tmp, JSON.stringify(payload));
  try {
    run(`dynamodb ${op} --cli-input-json file://${tmp} --output json`, region);
  } finally {
    unlinkSync(tmp);
  }
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
 */
function createApiKeyAndPlan({ name, envLabel, apiId, stage, rateLimit, burstLimit, quotaPerDay, region }) {
  const label = `idp-${name}-${envLabel}`;

  const keyOut = JSON.parse(run(`apigateway create-api-key --name "${label}" --enabled --output json`, region));
  const apiKeyId = keyOut.id;
  const apiKeyValue = run(
    `apigateway get-api-key --api-key ${apiKeyId} --include-value --query value --output text`,
    region
  );

  const planOut = JSON.parse(
    run(
      `apigateway create-usage-plan --name "${label}" ` +
        `--throttle rateLimit=${rateLimit},burstLimit=${burstLimit} ` +
        `--quota limit=${quotaPerDay},period=DAY ` +
        `--api-stages apiId=${apiId},stage=${stage} --output json`,
      region
    )
  );
  const usagePlanId = planOut.id;

  run(`apigateway create-usage-plan-key --usage-plan-id ${usagePlanId} --key-id ${apiKeyId} --key-type API_KEY --output json`, region);

  return { apiKeyId, apiKeyValue, usagePlanId };
}

function printNewKey({ name, apiKeyId, apiKeyValue, usagePlanId, rateLimit, burstLimit, quotaPerDay, region }) {
  console.log(`   ApiKeyId:    ${apiKeyId}`);
  console.log(`   UsagePlanId: ${usagePlanId}`);
  console.log(`   Rate limit:  ${rateLimit} req/s (burst ${burstLimit}), cuota ${quotaPerDay}/día`);
  console.log(`\n   🔑 Valor de la key (se puede reobtener cuando sea con):`);
  console.log(`      aws apigateway get-api-key --api-key ${apiKeyId} --include-value --query value --output text --region ${region}\n`);
  console.log(`   ${apiKeyValue}`);
  console.log(`\n   Ejemplo:`);
  console.log(`      curl -H "x-api-key: ${apiKeyValue}" <endpoint>/documents/...`);
  console.log(`\n   Entrégale este valor a '${name}' — no queda en ningún log de este script ni en DynamoDB (solo el ApiKeyId).`);
}

// Recomendación de rate/burst por tramos de volumen: en este dominio (subida
// de documentos, no llamadas de alta frecuencia) el volumen diario rara vez
// exige un rate/burst alto — lo que de verdad escala con el volumen es la
// cuota diaria. rate/burst se eligen por tramo (protegen contra un loop/burst
// anómalo, no contra tráfico legítimo normal); la cuota sí se calcula fino
// sobre el número dado, con margen para reintentos.
const VOLUME_TIERS = [
  { max: 50, rateLimit: 5, burstLimit: 10, label: "bajo (≤50/día)" },
  { max: 500, rateLimit: 10, burstLimit: 20, label: "medio (51-500/día)" },
  { max: 2000, rateLimit: 20, burstLimit: 40, label: "alto (501-2000/día)" },
  { max: 10000, rateLimit: 40, burstLimit: 80, label: "muy alto (2001-10000/día)" },
  { max: Infinity, rateLimit: 80, burstLimit: 160, label: "extremo (>10000/día)" },
];

function recommendLimits(docsPerDay) {
  const n = Math.max(0, Number(docsPerDay) || 0);
  const tier = VOLUME_TIERS.find((t) => n <= t.max);
  const quotaPerDay = Math.max(100, Math.ceil(n * 1.5)); // margen para reintentos/errores del cliente
  return { rateLimit: tier.rateLimit, burstLimit: tier.burstLimit, quotaPerDay, tierLabel: tier.label };
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

    const needRate = !args["rate-limit"];
    const needBurst = !args["burst-limit"];
    const needQuota = !args["quota-per-day"];
    if (needRate || needBurst || needQuota) {
      console.log("\nPara recomendar rate limit/cuota, cuéntame el volumen esperado.");
      const volAns = (
        await rl.question("¿Cuántos documentos por día esperas que suba esta app aproximadamente? (ej. 50, 500, 3000): ")
      ).trim();
      const rec = recommendLimits(Number(volAns) || 0);
      console.log(
        `\nRecomendado (categoría: ${rec.tierLabel}): rate=${rec.rateLimit}/s burst=${rec.burstLimit} cuota/día=${rec.quotaPerDay}` +
          " — Enter acepta el valor entre [corchetes], o escribe el tuyo.\n"
      );
      if (needRate) {
        args["rate-limit"] = (await rl.question(`Rate limit, req/s [${rec.rateLimit}]: `)).trim() || String(rec.rateLimit);
      }
      if (needBurst) {
        args["burst-limit"] = (await rl.question(`Burst [${rec.burstLimit}]: `)).trim() || String(rec.burstLimit);
      }
      if (needQuota) {
        args["quota-per-day"] = (await rl.question(`Cuota diaria [${rec.quotaPerDay}]: `)).trim() || String(rec.quotaPerDay);
      }
    }

    console.log(
      `\nResumen: app='${args.name}' env=${args.env} rate=${args["rate-limit"]}/s ` +
        `burst=${args["burst-limit"]} cuota/día=${args["quota-per-day"]}`
    );
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
  const rateLimit = Number(args["rate-limit"] || 20);
  const burstLimit = Number(args["burst-limit"] || 40);
  const quotaPerDay = Number(args["quota-per-day"] || 5000);
  // Validar TODO (incl. --access-scope) antes de tocar AWS — si esto corriera
  // después de crear la key/usage plan, un flag inválido dejaría recursos
  // huérfanos en AWS sin fila en DynamoDB que los referencie.
  const perms = parsePermissionFlags(args);

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
    name: args.name, envLabel, apiId, stage, rateLimit, burstLimit, quotaPerDay, region,
  });

  // Secure by default: AccessScope="own" (solo ve lo que ella subió) y
  // AllowFieldSearch=false (sin el Scan caro) salvo que se pida lo contrario
  // explícitamente — ver documentacion/AMBIENTES_Y_SEGURIDAD.md.
  ddbPutItem(
    table,
    {
      ApiKeyId: { S: apiKeyId },
      AppName: { S: args.name },
      UsagePlanId: { S: usagePlanId },
      Status: { S: "ACTIVE" },
      RateLimit: { N: String(rateLimit) },
      BurstLimit: { N: String(burstLimit) },
      QuotaPerDay: { N: String(quotaPerDay) },
      Environment: { S: envLabel },
      CreatedAt: { S: new Date().toISOString() },
      AccessScope: perms.setAttrs.AccessScope || { S: "own" },
      AllowFieldSearch: perms.setAttrs.AllowFieldSearch || { BOOL: false },
      ...(perms.setAttrs.AllowedDocTypes ? { AllowedDocTypes: perms.setAttrs.AllowedDocTypes } : {}),
    },
    region
  );

  console.log(`\n✅ App '${args.name}' registrada.`);
  printNewKey({ name: args.name, apiKeyId, apiKeyValue, usagePlanId, rateLimit, burstLimit, quotaPerDay, region });
  const scope = perms.setAttrs.AccessScope?.S || "own";
  console.log(`   Permisos: accessScope=${scope}, allowFieldSearch=${perms.setAttrs.AllowFieldSearch?.BOOL ?? false}` +
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
  // rotar no debería cambiar el rate limit de la app por accidente.
  const rateLimit = Number(args["rate-limit"] || old.RateLimit || 20);
  const burstLimit = Number(args["burst-limit"] || old.BurstLimit || 40);
  const quotaPerDay = Number(args["quota-per-day"] || old.QuotaPerDay || 5000);

  console.log(`🔄 Rotando '${args.name}' (${envLabel}) — key vieja ${old.ApiKeyId} quedará deshabilitada, no borrada.`);

  const apiId = resolveRestApiId(stack, region);
  const stage = resolveStage(apiId, region);
  const { apiKeyId, apiKeyValue, usagePlanId } = createApiKeyAndPlan({
    name: args.name, envLabel, apiId, stage, rateLimit, burstLimit, quotaPerDay, region,
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
      RateLimit: { N: String(rateLimit) },
      BurstLimit: { N: String(burstLimit) },
      QuotaPerDay: { N: String(quotaPerDay) },
      Environment: { S: envLabel },
      CreatedAt: { S: new Date().toISOString() },
      RotatedFrom: { S: old.ApiKeyId },
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

  console.log(`\n✅ '${args.name}' rotada. Key vieja (${old.ApiKeyId}) deshabilitada; consumidores deben actualizar a la nueva:`);
  printNewKey({ name: args.name, apiKeyId, apiKeyValue, usagePlanId, rateLimit, burstLimit, quotaPerDay, region });
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

    const needRate = args["rate-limit"] === undefined;
    const needBurst = args["burst-limit"] === undefined;
    const needQuota = args["quota-per-day"] === undefined;
    if (needRate || needBurst || needQuota) {
      console.log(
        `\nValores actuales de '${args.name}': rate=${app.RateLimit ?? "-"}/s ` +
          `burst=${app.BurstLimit ?? "-"} quota/día=${app.QuotaPerDay ?? "-"}`
      );
      console.log("Enter deja ese campo sin cambios; escribe un valor para reemplazarlo.\n");
      if (needRate) {
        const ans = (await rl.question(`Rate limit nuevo, req/s [sin cambio]: `)).trim();
        if (ans) args["rate-limit"] = ans;
      }
      if (needBurst) {
        const ans = (await rl.question(`Burst nuevo [sin cambio]: `)).trim();
        if (ans) args["burst-limit"] = ans;
      }
      if (needQuota) {
        const ans = (await rl.question(`Cuota diaria nueva [sin cambio]: `)).trim();
        if (ans) args["quota-per-day"] = ans;
      }
    }

    const summary =
      `rate=${args["rate-limit"] ?? app.RateLimit ?? "-"}/s ` +
      `burst=${args["burst-limit"] ?? app.BurstLimit ?? "-"} ` +
      `quota/día=${args["quota-per-day"] ?? app.QuotaPerDay ?? "-"}`;
    console.log(`\nResumen: '${args.name}' (${args.env}) → ${summary}`);
    await confirmOrExit(rl, "¿Aplicar estos cambios?");

    return { args, app };
  } finally {
    rl.close();
  }
}

async function cmdReconfigure(args) {
  const prompted = await promptReconfigure(args);
  args = prompted.args;

  if (!args.name) fail("Falta --name <appName>. Uso: pnpm apps:reconfigure -- --name <appName> [--rate-limit N] [--burst-limit N] [--quota-per-day N]");
  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const table = resolveAppsTable(args);

  const rateLimit = args["rate-limit"] !== undefined ? Number(args["rate-limit"]) : null;
  const burstLimit = args["burst-limit"] !== undefined ? Number(args["burst-limit"]) : null;
  const quotaPerDay = args["quota-per-day"] !== undefined ? Number(args["quota-per-day"]) : null;
  const perms = parsePermissionFlags(args);
  const hasPermChanges = Object.keys(perms.setAttrs).length > 0 || perms.removeAttrs.length > 0;
  if (rateLimit === null && burstLimit === null && quotaPerDay === null && !hasPermChanges) {
    fail(
      "Falta al menos uno de --rate-limit/--burst-limit/--quota-per-day/--access-scope/" +
        "--allowed-doctypes/--allow-field-search (esto solo cambia configuración, no crea ni revoca nada — usa 'rotate' para eso)."
    );
  }

  const app = prompted.app || findAppByName(table, args.name, region, { activeOnly: true });
  if (!app) {
    fail(
      `No hay ninguna app ACTIVE llamada '${args.name}' en ${table}. ` +
        `Usa 'pnpm apps:list -- --table ${table}' para ver qué apps existen.`
    );
  }

  // Los límites (throttle/quota) viven en el usage plan de API Gateway y
  // necesitan UsagePlanId; los permisos (AccessScope/AllowedDocTypes/
  // AllowFieldSearch) son solo atributos en DynamoDB — no dependen de él, así
  // que una app adoptada sin UsagePlanId conocido igual puede reconfigurar sus
  // permisos, solo no sus límites.
  const wantsLimitChange = rateLimit !== null || burstLimit !== null || quotaPerDay !== null;
  if (wantsLimitChange && !app.UsagePlanId) {
    fail(
      `'${args.name}' no tiene UsagePlanId registrado (probablemente adoptada sin uno con 'apps:adopt') — ` +
        `ajusta su usage plan a mano en la consola/CLI, este comando no puede inferir cuál es.`
    );
  }

  // Solo se tocan los campos pedidos — los que no se pasan se quedan como
  // estaban, tanto en API Gateway (JSON Patch por sub-path) como en DynamoDB.
  if (wantsLimitChange) {
    const patches = [];
    if (rateLimit !== null) patches.push(`op=replace,path=/throttle/rateLimit,value=${rateLimit}`);
    if (burstLimit !== null) patches.push(`op=replace,path=/throttle/burstLimit,value=${burstLimit}`);
    if (quotaPerDay !== null) patches.push(`op=replace,path=/quota/limit,value=${quotaPerDay}`);
    console.log(`⚙️  Reconfigurando límites de '${args.name}' (UsagePlanId ${app.UsagePlanId})...`);
    run(`apigateway update-usage-plan --usage-plan-id ${app.UsagePlanId} --patch-operations ${patches.join(" ")} --output json`, region);
  }

  const updates = { UpdatedAt: { S: new Date().toISOString() }, ...perms.setAttrs };
  if (rateLimit !== null) updates.RateLimit = { N: String(rateLimit) };
  if (burstLimit !== null) updates.BurstLimit = { N: String(burstLimit) };
  if (quotaPerDay !== null) updates.QuotaPerDay = { N: String(quotaPerDay) };
  ddbUpdateItem(table, { ApiKeyId: { S: app.ApiKeyId } }, updates, region);
  if (perms.removeAttrs.length) {
    ddbRemoveAttrs(table, { ApiKeyId: { S: app.ApiKeyId } }, perms.removeAttrs, region);
  }

  console.log(`\n✅ '${args.name}' reconfigurada. La API key NO cambió (${app.ApiKeyId}) — los consumidores no necesitan actualizar nada.`);
  if (wantsLimitChange) {
    console.log(
      `   rate=${rateLimit ?? app.RateLimit ?? "-"}/s burst=${burstLimit ?? app.BurstLimit ?? "-"} ` +
        `quota/día=${quotaPerDay ?? app.QuotaPerDay ?? "-"}`
    );
  }
  if (hasPermChanges) {
    console.log(
      `   accessScope=${perms.setAttrs.AccessScope?.S ?? app.AccessScope ?? "own"} ` +
        `allowFieldSearch=${perms.setAttrs.AllowFieldSearch?.BOOL ?? app.AllowFieldSearch ?? false}` +
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
  if (!args.name) fail("Falta --name <appName>. Uso: pnpm apps:grant -- --name <appName> [--doctype A,B] [--access-scope all] [--field-search]");

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
  if (!notes.length) {
    fail("Nada que otorgar — pasa --doctype A,B, --access-scope all, y/o --field-search.");
  }

  await confirmGrantDeny(args, "grant", notes);
  ddbUpdateItem(table, { ApiKeyId: { S: app.ApiKeyId } }, updates, region);
  console.log(`\n✅ Otorgado a '${args.name}': ${notes.join(", ")}.`);
}

async function cmdDeny(args) {
  const prompted = await promptGrantDeny(args, "deny");
  args = prompted.args;
  if (!args.name) fail("Falta --name <appName>. Uso: pnpm apps:deny -- --name <appName> [--doctype A,B] [--access-scope own] [--field-search]");

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
  if (!notes.length) {
    fail("Nada que quitar — pasa --doctype A,B, --access-scope own, y/o --field-search.");
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
    console.log(`     ApiKeyId: ${it.ApiKeyId}  UsagePlanId: ${it.UsagePlanId || "-"}`);
    console.log(
      `     rate=${it.RateLimit ?? "-"}/s burst=${it.BurstLimit ?? "-"} quota/día=${it.QuotaPerDay ?? "-"}  creada ${it.CreatedAt || "-"}`
    );
    console.log(
      `     accessScope=${it.AccessScope ?? "own"} allowFieldSearch=${it.AllowFieldSearch ?? false}` +
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
      run(`apigateway delete-usage-plan --usage-plan-id ${app.UsagePlanId} --output json`, region, { silent: true });
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
  let rateLimit = null;
  let burstLimit = null;
  let quotaPerDay = null;
  const plansOut = run(`apigateway get-usage-plans --key-id ${apiKeyId} --output json`, region, { silent: true });
  if (plansOut) {
    const plans = JSON.parse(plansOut).items || [];
    if (plans.length) {
      usagePlanId = plans[0].id;
      rateLimit = plans[0].throttle?.rateLimit ?? null;
      burstLimit = plans[0].throttle?.burstLimit ?? null;
      quotaPerDay = plans[0].quota?.limit ?? null;
    }
  }
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
      ...(rateLimit != null ? { RateLimit: { N: String(rateLimit) } } : {}),
      ...(burstLimit != null ? { BurstLimit: { N: String(burstLimit) } } : {}),
      ...(quotaPerDay != null ? { QuotaPerDay: { N: String(quotaPerDay) } } : {}),
      Environment: { S: envLabel },
      CreatedAt: { S: new Date().toISOString() },
    },
    region
  );

  console.log(`✅ '${apiKeyId}' adoptada como '${args.name}' (${envLabel}) en ${table}.`);
}

function printHelp() {
  console.log(`
Uso: pnpm apps:<comando> -- [opciones]

Comandos (todos interactivos en una terminal real si falta algo — siempre
piden confirmación S/n antes de tocar AWS, salvo --yes o sin TTY):
  register     --name X [--rate-limit 20] [--burst-limit 40] [--quota-per-day 5000] [--force]
               [--access-scope own|all] [--allowed-doctypes A,B] [--allow-field-search]
  rotate       --name X [--rate-limit N] [--burst-limit N] [--quota-per-day N]
  reconfigure  --name X [--rate-limit N] [--burst-limit N] [--quota-per-day N]
               [--access-scope own|all] [--allowed-doctypes A,B|none]
               [--allow-field-search] [--disallow-field-search]
  grant        --name X [--doctype A,B] [--access-scope all] [--field-search]
               (agrega/otorga SIN pisar lo que ya tenía — ver reconfigure para reemplazar todo)
  deny         --name X [--doctype A,B] [--access-scope own] [--field-search]
               (quita SOLO lo pedido — inverso de grant)
  disable      --name X                          (deshabilita la key, no la borra)
  enable       --name X                          (reactiva una app deshabilitada)
  revoke       --name X [--delete]                (= disable, + borra recursos AWS con --delete)
  adopt        --name X --api-key-id <ApiKeyId>
  list

Opciones comunes: --env dev|prod (default dev), --stack <NOMBRE>, --table <NOMBRE>, --region, --yes
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
