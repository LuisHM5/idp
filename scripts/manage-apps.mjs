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
 *   pnpm apps:list -- --env prod
 *   pnpm apps:revoke -- --name CEDIS --env dev
 *   pnpm apps:revoke -- --name CEDIS --env dev --delete   (borra también los recursos AWS, irreversible)
 *   pnpm apps:adopt -- --name legacy-idp-default --api-key-id <ApiKeyId> --env prod
 *
 * `register` es interactivo cuando corre en una terminal real y falta algún
 * dato: pregunta nombre/ambiente, y si falta rate-limit/burst-limit/cuota
 * pide el volumen de documentos esperado por día para RECOMENDAR valores
 * (ver `recommendLimits`/`VOLUME_TIERS`) — cualquier flag que ya pasaste no
 * se vuelve a preguntar. `--yes` fuerza el modo no interactivo (usa flags/
 * defaults tal cual, falla si falta algo requerido) — para scripting/CI.
 *
 * Opciones comunes:
 *   --env dev|prod     Ambiente (default: dev, igual que el deploy)
 *   --stack <NOMBRE>   Stack explícito (gana sobre --env)
 *   --table <NOMBRE>   Tabla Apps explícita (si no, se infiere de --env)
 *   --region           AWS region (default: AWS_REGION o us-east-1)
 *   --yes              No preguntar nada; usar flags/defaults tal cual (register)
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
    else out[k] = v;
  }
  return out;
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
 * Pregunta solo lo que falte en `args` (nada si ya viene todo por flags) y,
 * si hubo que preguntar algo, pide confirmación final antes de tocar AWS.
 * No hace nada si no hay TTY (pipe/CI) o si se pasó --yes — en esos casos
 * cmdRegister valida y falla igual que antes de que esto existiera.
 */
async function promptRegister(args) {
  if (!process.stdin.isTTY || args.yes) return args;

  let prompted = false;
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    if (!args.name) {
      prompted = true;
      args.name = (await rl.question("Nombre de la app (ej. CEDIS, ERP, Portal): ")).trim();
      if (!args.name) fail("El nombre no puede quedar vacío.");
    }

    if (!args.env) {
      prompted = true;
      const envAns = (await rl.question("¿Ambiente? (dev/prod) [dev]: ")).trim().toLowerCase();
      args.env = envAns === "prod" ? "prod" : "dev";
    }

    const needRate = !args["rate-limit"];
    const needBurst = !args["burst-limit"];
    const needQuota = !args["quota-per-day"];
    if (needRate || needBurst || needQuota) {
      prompted = true;
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

    if (prompted) {
      console.log(
        `\nResumen: app='${args.name}' env=${args.env} rate=${args["rate-limit"]}/s ` +
          `burst=${args["burst-limit"]} cuota/día=${args["quota-per-day"]}`
      );
      const confirm = (await rl.question("¿Crear esta app? (S/n): ")).trim().toLowerCase();
      if (confirm === "n" || confirm === "no") {
        console.log("Cancelado.");
        process.exit(0);
      }
    }
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
    },
    region
  );

  console.log(`\n✅ App '${args.name}' registrada.`);
  printNewKey({ name: args.name, apiKeyId, apiKeyValue, usagePlanId, rateLimit, burstLimit, quotaPerDay, region });
}

function cmdRotate(args) {
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
    if (it.RevokedAt) console.log(`     revocada ${it.RevokedAt}`);
    console.log("");
  }
}

function cmdRevoke(args) {
  if (!args.name) fail("Falta --name <appName>. Uso: pnpm apps:revoke -- --name <appName> --env dev|prod");
  const region = args.region || process.env.AWS_REGION || "us-east-1";
  const table = resolveAppsTable(args);

  const app = findAppByName(table, args.name, region);
  if (!app) fail(`No encontré ninguna app llamada '${args.name}' en ${table}.`);

  if (app.Status === "REVOKED") {
    console.log(`⚠️  '${args.name}' ya estaba revocada (${app.RevokedAt || "fecha desconocida"}).`);
  } else {
    run(`apigateway update-api-key --api-key ${app.ApiKeyId} --patch-operations op=replace,path=/enabled,value=false --output json`, region);
    ddbUpdateItem(
      table,
      { ApiKeyId: { S: app.ApiKeyId } },
      { Status: { S: "REVOKED" }, RevokedAt: { S: new Date().toISOString() } },
      region
    );
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

function cmdAdopt(args) {
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

Comandos:
  register --name X [--rate-limit 20] [--burst-limit 40] [--quota-per-day 5000] [--force]
  rotate   --name X [--rate-limit N] [--burst-limit N] [--quota-per-day N]
  list
  revoke   --name X [--delete]
  adopt    --name X --api-key-id <ApiKeyId>

Opciones comunes: --env dev|prod (default dev), --stack <NOMBRE>, --table <NOMBRE>, --region
`);
}

async function main() {
  const sub = process.argv[2];
  const args = parseArgs(process.argv.slice(3));

  switch (sub) {
    case "register":
      return await cmdRegister(args);
    case "rotate":
      return cmdRotate(args);
    case "list":
      return cmdList(args);
    case "revoke":
      return cmdRevoke(args);
    case "adopt":
      return cmdAdopt(args);
    default:
      printHelp();
      if (sub && sub !== "--help" && sub !== "-h") process.exitCode = 1;
  }
}

main();
