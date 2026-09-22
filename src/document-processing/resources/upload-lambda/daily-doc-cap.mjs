/**
 * Tope de documentos por día por app (`MaxDocsPerDay` en la tabla Apps,
 * fijado con `pnpm apps:register|reconfigure --docs-per-day N`).
 *
 * COPIA IDÉNTICA en upload-lambda/ y query-lambda/ — cada Lambda se empaqueta
 * como su propio asset (`Code.fromAsset`), así que no pueden compartir un
 * módulo; tests/unit/node/daily-doc-cap.test.mjs falla si las copias difieren.
 * Editar las dos a la vez.
 *
 * Por qué aquí y no en la cuota del usage plan: la cuota de API Gateway cuenta
 * TODAS las requests de la key, incluido el polling de GET /documents/{id}, y
 * no se puede excluir ningún método. Solo las rutas que disparan procesamiento
 * facturado (POST /documents, POST /upload, POST /documents/{id}/reprocess)
 * llaman a esto, así que los GET nunca gastan el presupuesto de documentos.
 *
 * Contador: una fila por (ApiKeyId, día UTC) en APP_USAGE_TABLE, incrementada
 * con un solo UpdateItem condicional — atómico aunque lleguen varios POST a la
 * vez, sin leer-y-luego-escribir. Día UTC, igual que la cuota de API Gateway.
 *
 * Fail-open: si DynamoDB falla por cualquier otra razón, se deja pasar el
 * documento (con log). Un problema del contador nunca debe tumbar las subidas;
 * la cuota del usage plan sigue como techo de respaldo.
 */

export const DAILY_LIMIT_ERROR = "DAILY_DOCUMENT_LIMIT";
// Las filas del contador son solo para `pnpm apps:usage`; TTL para que no crezcan sin fin.
const RETENTION_DAYS = 90;
const DAY_MS = 86400000;

/** YYYY-MM-DD en UTC. */
export function utcDay(now) {
  return now.toISOString().slice(0, 10);
}

/** Medianoche UTC siguiente a `now` — cuando se reinicia el contador. */
export function nextUtcMidnight(now) {
  const d = new Date(now.getTime());
  d.setUTCHours(24, 0, 0, 0);
  return d;
}

/** Límite válido (entero > 0) o null = sin tope. */
export function normalizeDailyLimit(value) {
  const n = Number(value);
  return Number.isInteger(n) && n > 0 ? n : null;
}

/**
 * Parámetros del UpdateItem condicional en formato AttributeValue (cliente de
 * bajo nivel, `UpdateItemCommand`): upload-lambda solo usa ese cliente y así
 * no depende de @aws-sdk/lib-dynamodb.
 */
export function dailyCapUpdateParams({ table, apiKeyId, appName, limit, now }) {
  return {
    TableName: table,
    Key: { ApiKeyId: { S: apiKeyId }, Day: { S: utcDay(now) } },
    UpdateExpression: "ADD #docs :one SET #app = :app, #exp = :exp",
    ConditionExpression: "attribute_not_exists(#docs) OR #docs < :max",
    ExpressionAttributeNames: { "#docs": "Docs", "#app": "AppName", "#exp": "ExpiresAt" },
    ExpressionAttributeValues: {
      ":one": { N: "1" },
      ":max": { N: String(limit) },
      ":app": { S: appName || "unknown" },
      ":exp": { N: String(Math.floor((now.getTime() + RETENTION_DAYS * DAY_MS) / 1000)) },
    },
    ReturnValues: "UPDATED_NEW",
  };
}

/**
 * Cuenta un documento contra el tope diario de la app.
 *   update(params) → Promise: ejecuta el UpdateItem (`UpdateItemCommand`).
 * Devuelve { allowed: true, ... } o { allowed: false, limit, resetsAt, retryAfterSeconds }.
 * Sin tabla configurada, sin API key o sin tope → allowed sin tocar DynamoDB.
 */
export async function consumeDailyDocument({
  update,
  table,
  apiKeyId,
  appName,
  maxDocsPerDay,
  now = new Date(),
  log = console,
}) {
  const limit = normalizeDailyLimit(maxDocsPerDay);
  if (!table || !apiKeyId || limit === null) return { allowed: true, counted: false };
  try {
    const out = await update(dailyCapUpdateParams({ table, apiKeyId, appName, limit, now }));
    const used = out?.Attributes?.Docs?.N;
    return { allowed: true, counted: true, used: used !== undefined ? Number(used) : null, limit };
  } catch (err) {
    if (err?.name === "ConditionalCheckFailedException") {
      const resetsAt = nextUtcMidnight(now);
      return {
        allowed: false,
        limit,
        resetsAt: resetsAt.toISOString(),
        retryAfterSeconds: Math.max(1, Math.ceil((resetsAt.getTime() - now.getTime()) / 1000)),
      };
    }
    log.error("Daily document cap check failed (failing open — document allowed):", err);
    return { allowed: true, counted: false, error: true };
  }
}

/** Respuesta 429 de API Gateway para un resultado `allowed: false`. */
export function dailyLimitResponse(result) {
  return {
    statusCode: 429,
    headers: { "Content-Type": "application/json", "Retry-After": String(result.retryAfterSeconds) },
    body: JSON.stringify({
      error: DAILY_LIMIT_ERROR,
      message:
        `This app reached its limit of ${result.limit} documents per day (UTC). ` +
        "Reads (GET) are not affected. The counter resets at resetsAt.",
      limit: result.limit,
      resetsAt: result.resetsAt,
      retryAfterSeconds: result.retryAfterSeconds,
    }),
  };
}
