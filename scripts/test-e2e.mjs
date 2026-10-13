#!/usr/bin/env node
/**
 * E2E real del contrato público:
 *
 *   POST /documents -> PUT directo a S3 -> GET /documents/{id} hasta FINISHED
 *
 * Recibe un archivo real y el TYPE_ID declarado. Los mensajes de progreso van
 * a stderr; stdout contiene exclusivamente el JSON final que devolvió el GET,
 * de modo que el resultado se puede redirigir o encadenar a `jq`.
 *
 * Uso recomendado:
 *   pnpm test-e2e -- ./documento.pdf FACTURA_FLETE_EXTRANJERO
 *   pnpm test-e2e -- ./documento.pdf FACTURA_FLETE_EXTRANJERO --env prod
 *   pnpm test-e2e -- --file ./documento.pdf --type FACTURA_FLETE_EXTRANJERO
 */

import { execFileSync } from "node:child_process";
import { createReadStream, existsSync, readFileSync, statSync } from "node:fs";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { basename, extname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const DEV_STACK = "DocumentValidationStack-Dev";
const PROD_STACK = "DocumentValidationStack";
const DEFAULT_TIMEOUT_SECONDS = 20 * 60;
const REQUEST_TIMEOUT_MS = 30_000;
const ALLOWED_EXTENSIONS = new Set(["jpg", "jpeg", "png", "pdf", "xml", "xlsx", "csv"]);
// Un tipo, o varios separados por coma — el mismo contrato que acepta
// `POST /documents` (ver upload-lambda/body-parser.mjs::parseDocumentTypes).
// Aquí solo se valida la FORMA; los topes de 8 tipos / 200 caracteres los
// aplica la API, para no tener dos fuentes de verdad de los límites.
const DOCUMENT_TYPE_PATTERN = /^[A-Z][A-Z0-9_]{0,63}(?::(?:[1-9][0-9]*|[aA][lL][lL]))?(?:,[A-Z][A-Z0-9_]{0,63}(?::(?:[1-9][0-9]*|[aA][lL][lL]))?)*$/;
const SCRIPT_DIR = fileURLToPath(new URL(".", import.meta.url));
const REPO_ROOT = resolve(SCRIPT_DIR, "..");

export class E2EError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = "E2EError";
    Object.assign(this, details);
  }
}

/** Lee una variable dotenv sin cargar ni alterar el entorno del proceso. */
export function readDotEnvVar(path, key) {
  if (!existsSync(path)) return null;
  let value = null;
  for (const raw of readFileSync(path, "utf8").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const idx = line.indexOf("=");
    if (line.slice(0, idx).trim() === key) {
      value = line.slice(idx + 1).trim().replace(/^["']|["']$/g, "");
    }
  }
  return value;
}

export function parseArgs(argv) {
  const options = { positionals: [], full: false };
  const valueOptions = new Set([
    "file", "type", "doc-type", "occurrence-mode", "env", "endpoint", "stack", "region", "timeout", "wait",
  ]);

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--") continue;
    if (arg === "--help" || arg === "-h") {
      options.help = true;
      continue;
    }
    if (arg === "--full") {
      options.full = true;
      continue;
    }
    if (arg === "-f" || arg === "-t") {
      const key = arg === "-f" ? "file" : "type";
      if (!argv[i + 1] || argv[i + 1].startsWith("-")) {
        throw new E2EError(`Falta el valor de ${arg}`);
      }
      options[key] = argv[++i];
      continue;
    }
    if (arg.startsWith("--")) {
      const key = arg.slice(2);
      if (!valueOptions.has(key)) throw new E2EError(`Opción desconocida: ${arg}`);
      if (!argv[i + 1] || argv[i + 1].startsWith("--")) {
        throw new E2EError(`Falta el valor de ${arg}`);
      }
      options[key] = argv[++i];
      continue;
    }
    options.positionals.push(arg);
  }

  if (!options.file && options.positionals.length) options.file = options.positionals.shift();
  if (!options.type && !options["doc-type"] && options.positionals.length) {
    options.type = options.positionals.shift();
  }
  if (options.positionals.length) {
    throw new E2EError(`Argumento inesperado: ${options.positionals[0]}`);
  }
  options.type = options.type || options["doc-type"];
  options.occurrenceMode = options["occurrence-mode"]?.toLowerCase();
  delete options["occurrence-mode"];
  options.timeout = options.timeout || options.wait;
  return options;
}

export function printHelp(stream = process.stdout) {
  stream.write(`
Uso:
  pnpm test-e2e -- <documento> <TIPO_DOCUMENTO> [opciones]
  pnpm test-e2e -- --file <documento> --type <TIPO_DOCUMENTO> [opciones]

Ejecuta el flujo real POST /documents -> PUT S3 -> GET /documents/{id} y
escribe en stdout exactamente el JSON final devuelto por GET.

Parámetros:
  documento             Ruta al archivo real
  TIPO_DOCUMENTO        TYPE_ID en UPPER_SNAKE_CASE. Varios separados por coma
                        (PEDIMENTO_ADUANAL,FACTURA_HONORARIOS) hacen que el
                        archivo se segmente y cada documento se clasifique
                        solo entre esos tipos y se omite el resto. :N/:all
                        sobrescribe --occurrence-mode para ese tipo.

Opciones:
  --env dev|prod        Ambiente (default: dev)
  --endpoint URL        API explícita; evita consultar CloudFormation
  --stack NOMBRE        Stack explícito; gana sobre --env
  --region REGION       Región AWS (default: AWS_REGION o us-east-1)
  --timeout SEGUNDOS    Espera máxima total (default: ${DEFAULT_TIMEOUT_SECONDS})
  --occurrence-mode MODO
                        single conserva la primera ocurrencia de cada tipo sin
                        sufijo; multiple conserva todas. Si se omite: catálogo;
                        después single con un tipo, multiple con varios
  --full                Añade ?full=1 al GET final
  --help                Muestra esta ayuda

Ejemplos:
  pnpm test-e2e -- ./factura.pdf FACTURA_FLETE_EXTRANJERO
  pnpm test-e2e -- ./facturas.pdf FACTURA_FLETE_EXTRANJERO --occurrence-mode multiple
  pnpm test-e2e -- ./pedimento.pdf PEDIMENTO_ADUANAL --env prod
  pnpm --silent test-e2e -- ./factura.pdf FACTURA_FLETE_EXTRANJERO > resultado.json
`);
}

export function getEndpointFromStack(stackName, region) {
  try {
    const output = execFileSync(
      "aws",
      [
        "cloudformation", "describe-stacks",
        "--stack-name", stackName,
        "--region", region,
        "--query", "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue",
        "--output", "text",
      ],
      { encoding: "utf8", timeout: REQUEST_TIMEOUT_MS, stdio: ["ignore", "pipe", "pipe"] }
    ).trim();
    return output && output !== "None" ? output.replace(/\/+$/, "") : null;
  } catch {
    return null;
  }
}

/** Misma política de credenciales que `scripts/enviar-documento.sh`. */
export function resolveApiKey(env, environment = process.env) {
  if (environment.API_KEY) return environment.API_KEY;
  const dotenv = join(REPO_ROOT, env === "prod" ? ".env.production" : ".env.dev");

  if (env === "prod") {
    return environment.API_KEY_APP
      || readDotEnvVar(dotenv, "API_KEY")
      || readDotEnvVar(dotenv, "API_KEY_APP");
  }

  const required = environment.DEV_API_KEY_REQUIRED
    || readDotEnvVar(dotenv, "DEV_API_KEY_REQUIRED")
    || "";
  if (required.trim().toLowerCase() !== "true") return null;
  return environment.API_KEY_APP || readDotEnvVar(dotenv, "API_KEY_APP");
}

function apiHeaders(apiKey, includeJson = false) {
  return {
    ...(includeJson && { "Content-Type": "application/json" }),
    ...(apiKey && { "x-api-key": apiKey }),
  };
}

async function responseBody(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    throw new E2EError(`La API devolvió un cuerpo que no es JSON (HTTP ${response.status})`, {
      statusCode: response.status,
      responseText: text,
    });
  }
}

async function apiFetch(url, options = {}, fetchImpl = fetch) {
  let response;
  try {
    response = await fetchImpl(url, {
      ...options,
      signal: options.signal || AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (error) {
    throw new E2EError(`No se pudo conectar a ${url}: ${error.message}`, { cause: error });
  }
  const body = await responseBody(response);
  return { response, body };
}

/**
 * Explica un 429 en español. Hay tres, y solo uno se arregla esperando unos
 * segundos (ver documentacion/api-docs/API_USO.md → "Límites y 429"):
 *   - "Too Many Requests": throttle (req/s) de API Gateway → reintentar.
 *   - "Limit Exceeded": cuota diaria de requests de la API key → hasta mañana (UTC).
 *   - DAILY_DOCUMENT_LIMIT: tope de documentos/día de la app → hasta `resetsAt`.
 */
export function describeRateLimit(body) {
  if (body?.error === "DAILY_DOCUMENT_LIMIT") {
    return `tope de documentos por día alcanzado (${body.limit}); se reinicia ${body.resetsAt}`;
  }
  if (body?.message === "Limit Exceeded") {
    return "cuota diaria de requests de la API key agotada; se reinicia a medianoche UTC";
  }
  return "throttle de API Gateway (demasiadas requests por segundo)";
}

/** Solo el throttle por segundo se resuelve reintentando pronto. */
function isRetryableThrottle(response, body) {
  return response.status === 429 && body?.error !== "DAILY_DOCUMENT_LIMIT" && body?.message !== "Limit Exceeded";
}

export async function registerDocument({
  endpoint, filePath, documentType, occurrenceMode, apiKey, metadata, fetchImpl,
}) {
  const { response, body } = await apiFetch(`${endpoint}/documents`, {
    method: "POST",
    headers: apiHeaders(apiKey, true),
    body: JSON.stringify({
      fileName: basename(filePath),
      documentType,
      ...(occurrenceMode && { occurrenceMode }),
      metadata: metadata || { source: "test-e2e" },
    }),
  }, fetchImpl);

  if (response.status === 429) {
    throw new E2EError(`POST /documents rechazado (HTTP 429, ${describeRateLimit(body)}): ${JSON.stringify(body)}`, {
      statusCode: response.status,
      responseBody: body,
    });
  }
  if (response.status !== 200 || !body?.documentId || !body?.uploadUrl) {
    throw new E2EError(`POST /documents falló (HTTP ${response.status}): ${JSON.stringify(body)}`, {
      statusCode: response.status,
      responseBody: body,
    });
  }
  return body;
}

/** Sube en streaming: el documento no se carga completo en memoria. */
export function putFile(uploadUrl, filePath) {
  return new Promise((resolvePromise, reject) => {
    const url = new URL(uploadUrl);
    const request = url.protocol === "https:" ? httpsRequest : httpRequest;
    const req = request(
      url,
      {
        method: "PUT",
        // Es una cabecera de transporte calculada por el cliente, no metadata
        // x-amz. El PUT sigue sin reenviar x-api-key ni cabeceras del POST.
        headers: { "Content-Length": statSync(filePath).size },
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          const body = Buffer.concat(chunks).toString("utf8");
          if (response.statusCode >= 200 && response.statusCode < 300) {
            resolvePromise();
          } else {
            reject(new E2EError(`PUT a S3 falló (HTTP ${response.statusCode}): ${body}`, {
              statusCode: response.statusCode,
              responseText: body,
            }));
          }
        });
      }
    );
    req.setTimeout(5 * 60_000, () => req.destroy(new Error("timeout del PUT a S3")));
    req.on("error", (error) => reject(new E2EError(`No se pudo subir el archivo: ${error.message}`, { cause: error })));
    const stream = createReadStream(filePath);
    stream.on("error", (error) => req.destroy(error));
    stream.pipe(req);
  });
}

function retryAfterMilliseconds(response, body) {
  const raw = response.headers.get("retry-after");
  if (raw !== null && /^\d+(?:\.\d+)?$/.test(raw.trim())) return Number(raw) * 1000;
  if (raw) {
    const date = Date.parse(raw);
    if (Number.isFinite(date)) return Math.max(0, date - Date.now());
  }
  const fromBody = Number(body?.retryAfterSeconds);
  return Number.isFinite(fromBody) && fromBody >= 0 ? fromBody * 1000 : 1000;
}

export async function pollDocument({
  endpoint,
  documentId,
  apiKey,
  timeoutMs,
  full = false,
  sleep = (ms) => new Promise((resolvePromise) => setTimeout(resolvePromise, ms)),
  jitter = () => Math.random() * 250,
  onPoll = () => {},
  fetchImpl,
}) {
  const startedAt = Date.now();
  const deadline = startedAt + timeoutMs;
  let backoffMs = 1000;
  let attempt = 0;

  while (Date.now() <= deadline) {
    attempt++;
    const query = new URLSearchParams();
    if (full) query.set("full", "1");
    // One bounded long-poll removes the old fixed five-second blind spot for
    // short documents without changing GET's default behavior for clients.
    if (attempt === 1) query.set("waitSeconds", "10");
    const suffix = query.size ? `?${query}` : "";
    const { response, body } = await apiFetch(
      `${endpoint}/documents/${encodeURIComponent(documentId)}${suffix}`,
      { headers: apiHeaders(apiKey) },
      fetchImpl
    );

    if (response.status === 200) {
      onPoll({ attempt, statusCode: 200, processingStatus: body?.processingStatus, waitMs: 0 });
      return body;
    }
    // Un 429 de throttle en el polling no es un fallo: se espera como un 202
    // (mismo backoff). La cuota diaria agotada sí es fatal — reintentar solo
    // seguiría recibiendo 429 hasta la medianoche UTC.
    const throttled = isRetryableThrottle(response, body);
    if (response.status !== 202 && !throttled) {
      const reason = response.status === 429 ? ` (${describeRateLimit(body)})` : "";
      throw new E2EError(`GET /documents/${documentId} falló (HTTP ${response.status}${reason}): ${JSON.stringify(body)}`, {
        statusCode: response.status,
        responseBody: body,
      });
    }

    const minimumMs = retryAfterMilliseconds(response, body);
    const waitMs = Math.max(backoffMs, minimumMs) + Math.max(0, jitter());
    onPoll({ attempt, statusCode: response.status, processingStatus: body?.processingStatus, waitMs });
    if (Date.now() + waitMs > deadline) break;
    await sleep(waitMs);
    backoffMs = Math.min(backoffMs * 2, 30_000);
  }

  throw new E2EError(
    `Tiempo agotado después de ${Math.round((Date.now() - startedAt) / 1000)}s esperando ${documentId}`,
    { documentId }
  );
}

export async function runE2E({
  endpoint,
  filePath,
  documentType,
  occurrenceMode,
  apiKey = null,
  timeoutMs = DEFAULT_TIMEOUT_SECONDS * 1000,
  full = false,
  log = () => {},
  sleep,
  jitter,
  fetchImpl,
  putFileImpl = putFile,
}) {
  log(`POST /documents · ${basename(filePath)} · ${documentType}`);
  const registration = await registerDocument({
    endpoint, filePath, documentType, occurrenceMode, apiKey, fetchImpl,
  });
  log(`Registrado ${registration.documentId} (${registration.processingStatus || "AWAITING_UPLOAD"})`);

  log("PUT directo a S3");
  await putFileImpl(registration.uploadUrl, filePath);
  log("Archivo subido; comienza el polling del API");

  let lastStatus = null;
  const finalResponse = await pollDocument({
    endpoint,
    documentId: registration.documentId,
    apiKey,
    timeoutMs,
    full,
    ...(sleep && { sleep }),
    ...(jitter && { jitter }),
    ...(fetchImpl && { fetchImpl }),
    onPoll: ({ statusCode, processingStatus, waitMs }) => {
      if (statusCode === 429) {
        log(`429 throttle de API Gateway · reintento en ${(waitMs / 1000).toFixed(1)}s`);
        return;
      }
      if (processingStatus !== lastStatus) {
        log(`${processingStatus || "ESTADO_DESCONOCIDO"}${waitMs ? ` · próximo GET en ${(waitMs / 1000).toFixed(1)}s` : ""}`);
        lastStatus = processingStatus;
      }
    },
  });

  if (finalResponse?.documentId !== registration.documentId) {
    throw new E2EError("El GET final devolvió un documentId distinto al registrado", {
      expectedDocumentId: registration.documentId,
      actualDocumentId: finalResponse?.documentId,
    });
  }
  return finalResponse;
}

function resolveInputFile(rawPath, environment = process.env) {
  if (!rawPath) return null;
  if (isAbsolute(rawPath) || existsSync(rawPath)) return resolve(rawPath);
  if (environment.INIT_CWD && existsSync(join(environment.INIT_CWD, rawPath))) {
    return resolve(environment.INIT_CWD, rawPath);
  }
  return resolve(rawPath);
}

export function validateOptions(options) {
  if (!options.file) throw new E2EError("Falta el documento");
  if (!options.type) throw new E2EError("Falta TIPO_DOCUMENTO");
  if (!existsSync(options.file) || !statSync(options.file).isFile()) {
    throw new E2EError(`No existe el archivo: ${options.file}`);
  }
  const extension = extname(options.file).slice(1).toLowerCase();
  if (!ALLOWED_EXTENSIONS.has(extension)) {
    throw new E2EError(`Formato no soportado: .${extension || "(sin extensión)"}`);
  }
  if (!DOCUMENT_TYPE_PATTERN.test(options.type)) {
    throw new E2EError(
      "TIPO_DOCUMENTO debe usar UPPER_SNAKE_CASE y tener máximo 64 caracteres. " +
        "Para varios tipos, sepáralos por coma sin espacios: " +
        "PEDIMENTO_ADUANAL,FACTURA_HONORARIOS"
    );
  }
  if (options.occurrenceMode && !new Set(["single", "multiple"]).has(options.occurrenceMode)) {
    throw new E2EError("--occurrence-mode debe ser single o multiple");
  }
  if (!Number.isFinite(options.timeoutSeconds) || options.timeoutSeconds <= 0) {
    throw new E2EError("--timeout debe ser un número positivo de segundos");
  }
}

export async function main(argv = process.argv.slice(2), environment = process.env) {
  const options = parseArgs(argv);
  if (options.help) {
    printHelp();
    return 0;
  }

  const env = options.env || "dev";
  if (!new Set(["dev", "prod"]).has(env)) throw new E2EError("--env debe ser dev o prod");
  const region = options.region || environment.AWS_REGION || "us-east-1";
  const stackName = options.stack || (env === "prod" ? PROD_STACK : DEV_STACK);
  options.file = resolveInputFile(options.file, environment);
  options.timeoutSeconds = Number(options.timeout || DEFAULT_TIMEOUT_SECONDS);
  validateOptions(options);

  const endpoint = (options.endpoint || environment.API_ENDPOINT || getEndpointFromStack(stackName, region))
    ?.replace(/\/+$/, "");
  if (!endpoint) {
    throw new E2EError(
      `No se pudo obtener ApiEndpoint de ${stackName}; usa --endpoint URL o autentica AWS CLI`
    );
  }
  let parsedEndpoint;
  try {
    parsedEndpoint = new URL(endpoint);
  } catch {
    throw new E2EError(`Endpoint inválido: ${endpoint}`);
  }
  if (!new Set(["http:", "https:"]).has(parsedEndpoint.protocol)) {
    throw new E2EError(`Endpoint inválido: ${endpoint}`);
  }

  const apiKey = resolveApiKey(env, environment);
  if (env === "prod" && !apiKey) {
    throw new E2EError("Producción requiere API_KEY o API_KEY_APP (shell o .env.production)");
  }
  if (env === "dev") {
    const required = environment.DEV_API_KEY_REQUIRED
      || readDotEnvVar(join(REPO_ROOT, ".env.dev"), "DEV_API_KEY_REQUIRED")
      || "";
    if (required.trim().toLowerCase() === "true" && !apiKey) {
      throw new E2EError("Dev exige API key, pero no existe API_KEY/API_KEY_APP en el entorno ni en .env.dev");
    }
  }

  const log = (message) => process.stderr.write(`[e2e] ${message}\n`);
  log(`API ${endpoint} · timeout ${options.timeoutSeconds}s`);
  const finalResponse = await runE2E({
    endpoint,
    filePath: options.file,
    documentType: options.type,
    occurrenceMode: options.occurrenceMode,
    apiKey,
    timeoutMs: options.timeoutSeconds * 1000,
    full: options.full,
    log,
  });

  // stdout del script queda deliberadamente libre de logs. Al invocarlo con
  // pnpm usa --silent para que el banner del package manager tampoco se mezcle
  // con el JSON: `pnpm --silent test-e2e -- ... > resultado.json`.
  process.stdout.write(`${JSON.stringify(finalResponse, null, 2)}\n`);
  if (finalResponse?.processingStatus !== "FINISHED") {
    process.stderr.write(`[e2e] Estado terminal inesperado: ${finalResponse?.processingStatus || "ausente"}\n`);
    return 1;
  }
  if (!finalResponse.result) {
    process.stderr.write("[e2e] FINISHED sin result\n");
    return 1;
  }
  return 0;
}

const isDirectExecution = process.argv[1]
  && import.meta.url === pathToFileURL(resolve(process.argv[1])).href;
if (isDirectExecution) {
  main().then(
    (exitCode) => { process.exitCode = exitCode; },
    (error) => {
      process.stderr.write(`[e2e] ERROR: ${error.message}\n`);
      process.exitCode = 1;
    }
  );
}
