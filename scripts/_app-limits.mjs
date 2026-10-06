/**
 * Límites por app — helpers PUROS de scripts/manage-apps.mjs. Viven aparte
 * porque manage-apps ejecuta `main()` al importarse; esto sí se puede
 * importar desde tests (tests/unit/node/app-limits.test.mjs).
 *
 * Cada app tiene tres límites independientes (ver
 * documentacion/AMBIENTES_Y_SEGURIDAD.md → "Límites por app"):
 *   1. Throttle de LECTURA (req/s + burst): el throttle del usage plan de
 *      API Gateway. Aplica a los GET (status polling, /file, búsquedas,
 *      catálogo de tipos).
 *   2. Throttle de ESCRITURA: override por método del MISMO usage plan en
 *      `WRITE_METHODS`, normalmente más bajo que el de lectura.
 *   3. Documentos/día (`MaxDocsPerDay` en la tabla Apps): lo cuenta la Lambda
 *      (daily-doc-cap.mjs) solo en las rutas que disparan procesamiento
 *      facturado. La cuota diaria del usage plan cuenta TODAS las requests
 *      (incluido el polling), así que queda como techo anti-abuso amplio, no
 *      como "documentos por día".
 *
 * Por qué lectura = throttle del plan y escritura = override, y no al revés:
 * AWS no documenta si un override por método dentro de un usage plan puede
 * SUBIR por encima del throttle del plan; BAJAR sí funciona siempre. Con este
 * reparto el resultado es correcto con cualquiera de las dos semánticas.
 */

/**
 * Métodos que llevan el throttle de escritura, con la misma clave que usa
 * API Gateway en `apiStages[].throttle` (`${resource.path}/${httpMethod}`,
 * igual que renderThrottlePerMethod de aws-cdk-lib). Cualquier método no-GET
 * nuevo en src/document-validation-stack.ts debe agregarse aquí — lo verifica
 * tests/unit/node/app-limits.test.mjs.
 */
export const WRITE_METHODS = Object.freeze([
  "/documents/POST",
  "/upload/POST",
  "/documents/{id}/reprocess/POST",
  "/documents/{id}/metadata/PATCH",
  // Al final a propósito: `effectiveLimits` sondea WRITE_METHODS[0].
  "/references/{scope}/{key}/POST",
  "/references/{scope}/{key}/{kind}/{refId}/DELETE",
  "/references/{scope}/{key}/{kind}/{refId}/confirm/POST",
]);

/** Recomendación de lectura vs escritura: los GET son baratos (Lambda + DynamoDB). */
export const READ_MULTIPLIER = 4;

/**
 * Requests por documento para dimensionar la cuota (techo) del usage plan:
 * 1 POST + polling de GET /documents/{id} con `Retry-After: 5` durante el p95
 * de procesamiento medido en dev (~238 s el 2026-09-21 → ~48 GETs) + /file y
 * PATCH metadata.
 */
export const REQUESTS_PER_DOC = 50;
export const MIN_QUOTA_PER_DAY = 1000;
export const MIN_DOCS_PER_DAY = 20;

// Recomendación de rate/burst de escritura por tramos de volumen: en este
// dominio (subida de documentos, no llamadas de alta frecuencia) el volumen
// diario rara vez exige un rate/burst alto — lo que escala con el volumen es
// el tope de documentos/día. rate/burst se eligen por tramo (protegen contra
// un loop/burst anómalo, no contra tráfico legítimo normal).
export const VOLUME_TIERS = Object.freeze([
  { max: 50, rateLimit: 5, burstLimit: 10, label: "bajo (≤50/día)" },
  { max: 500, rateLimit: 10, burstLimit: 20, label: "medio (51-500/día)" },
  { max: 2000, rateLimit: 20, burstLimit: 40, label: "alto (501-2000/día)" },
  { max: 10000, rateLimit: 40, burstLimit: 80, label: "muy alto (2001-10000/día)" },
  { max: Infinity, rateLimit: 80, burstLimit: 160, label: "extremo (>10000/día)" },
]);

/** Throttle de lectura recomendado a partir del de escritura. */
export function recommendReadLimits({ rateLimit, burstLimit }) {
  return {
    readRateLimit: rateLimit * READ_MULTIPLIER,
    readBurstLimit: burstLimit * READ_MULTIPLIER,
  };
}

/** Recomendación completa a partir de los documentos/día esperados. */
export function recommendLimits(expectedDocsPerDay) {
  const n = Math.max(0, Number(expectedDocsPerDay) || 0);
  const tier = VOLUME_TIERS.find((t) => n <= t.max);
  // Margen de 50% para reintentos/reprocesos del cliente.
  const docsPerDay = Math.max(MIN_DOCS_PER_DAY, Math.ceil(n * 1.5));
  return {
    rateLimit: tier.rateLimit,
    burstLimit: tier.burstLimit,
    ...recommendReadLimits(tier),
    docsPerDay,
    // Siempre por encima del tope de documentos + su polling, para que el
    // límite que el cliente vea primero sea el explícito (DAILY_DOCUMENT_LIMIT)
    // y no la cuota ciega de API Gateway.
    quotaPerDay: Math.max(MIN_QUOTA_PER_DAY, docsPerDay * REQUESTS_PER_DOC),
    tierLabel: tier.label,
  };
}

/**
 * Valida un flag numérico de límite. Devuelve `{ value }` o `{ error }`.
 * `integer`: burst, cuota y documentos/día son enteros en AWS/DynamoDB; el
 * rate de API Gateway admite decimales.
 */
export function parseLimit(raw, label, { integer = false } = {}) {
  const value = Number(raw);
  if (raw === undefined || raw === null || raw === "" || raw === true || !Number.isFinite(value) || value <= 0) {
    return { error: `${label} debe ser un número mayor que 0 (recibido: ${raw}).` };
  }
  if (integer && !Number.isInteger(value)) {
    return { error: `${label} debe ser un entero (recibido: ${raw}).` };
  }
  return { value };
}

/** `{ "/documents/POST": { rateLimit, burstLimit }, ... }` para `apiStages[].throttle`. */
export function writeThrottleMap({ rateLimit, burstLimit }) {
  return Object.fromEntries(WRITE_METHODS.map((m) => [m, { rateLimit, burstLimit }]));
}

/** Payload de `aws apigateway create-usage-plan --cli-input-json`. */
export function usagePlanInput({ name, apiId, stage, write, read, quotaPerDay }) {
  return {
    name,
    throttle: { rateLimit: read.rateLimit, burstLimit: read.burstLimit },
    quota: { limit: quotaPerDay, period: "DAY" },
    apiStages: [{ apiId, stage, throttle: writeThrottleMap(write) }],
  };
}

/**
 * Patch path de un campo de throttle por método: la clave del mapa
 * ("/documents/{id}/reprocess/POST") va TAL CUAL después de `/throttle`, sin
 * escapar. A diferencia de UpdateStage, UpdateUsagePlan NO desescapa "~1":
 * todo lo que hay entre `throttle` y `/<METHOD>/<campo>` es el resourcePath
 * literal — medido contra AWS el 2026-09-21: `throttle/~1documents/POST`
 * falla con `Invalid method {resourcePath: /~1documents, method: POST}`.
 */
export function methodThrottlePath(apiId, stage, methodKey, field) {
  return `/apiStages/${apiId}:${stage}/throttle${methodKey}/${field}`;
}

/**
 * Límites vigentes de un usage plan real (salida de `get-usage-plan`).
 * `legacy: true` = plan creado antes de separar lectura/escritura: sin
 * overrides, así que GET y escrituras comparten el throttle del plan.
 */
export function effectiveLimits(plan) {
  const planThrottle = {
    rateLimit: plan?.throttle?.rateLimit ?? null,
    burstLimit: plan?.throttle?.burstLimit ?? null,
  };
  const overrides = plan?.apiStages?.[0]?.throttle || {};
  const writeOverride = overrides[WRITE_METHODS[0]];
  return {
    legacy: !writeOverride,
    read: planThrottle,
    write: writeOverride
      ? { rateLimit: writeOverride.rateLimit ?? null, burstLimit: writeOverride.burstLimit ?? null }
      : planThrottle,
    quotaPerDay: plan?.quota?.limit ?? null,
  };
}

/**
 * Patch operations de `update-usage-plan` que dejan el plan con estos límites.
 * `read`/`write` deben venir COMPLETOS (rate y burst) — el caller resuelve lo
 * no pedido desde `effectiveLimits(plan)` — porque un override nuevo necesita
 * ambos campos. Usa "add" para overrides que el plan aún no tiene (plan
 * legacy o método nuevo) y "replace" para los existentes; se aplica a cada
 * apiStage del plan. Cualquiera de read/write/quotaPerDay puede omitirse.
 */
export function usagePlanPatchOps(plan, { read, write, quotaPerDay } = {}) {
  const ops = [];
  if (read) {
    ops.push({ op: "replace", path: "/throttle/rateLimit", value: String(read.rateLimit) });
    ops.push({ op: "replace", path: "/throttle/burstLimit", value: String(read.burstLimit) });
  }
  if (quotaPerDay != null) {
    ops.push({ op: "replace", path: "/quota/limit", value: String(quotaPerDay) });
  }
  if (write) {
    for (const apiStage of plan?.apiStages || []) {
      const existing = apiStage.throttle || {};
      for (const method of WRITE_METHODS) {
        const op = existing[method] ? "replace" : "add";
        for (const field of ["rateLimit", "burstLimit"]) {
          ops.push({
            op,
            path: methodThrottlePath(apiStage.apiId, apiStage.stage, method, field),
            value: String(write[field]),
          });
        }
      }
    }
  }
  return ops;
}

/** "rate/s (burst N)" o "-" si falta algún dato. */
export function formatThrottle({ rateLimit, burstLimit } = {}) {
  if (rateLimit == null || burstLimit == null) return "-";
  return `${rateLimit}/s (burst ${burstLimit})`;
}
