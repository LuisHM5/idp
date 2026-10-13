/**
 * UploadLambda - Document upload handler.
 *
 * ONE flow, always: `POST /documents` never carries the file (`/upload` is a
 * compatibility alias). It validates the
 * request, mints the documentId and S3 key, and returns a short-lived
 * presigned S3 `PUT` URL. The caller then uploads the bytes straight to S3,
 * and the pipeline starts when S3 receives the object.
 *
 * Why the bytes never come through here: API Gateway caps a request at 10MB
 * and Lambda caps a synchronous invoke payload at 6MB — both hard AWS limits,
 * neither configurable — and base64 inflates a file by ~33%, so an inline
 * upload silently died somewhere above ~4MB of real file. That made request
 * SIZE decide which code path ran, which is the kind of split that passes
 * every test and then fails on the first document that happens to be too big.
 * Going through S3 always removes the cliff: a 200KB CFDI and a 300MB
 * expediente take the identical two steps.
 *
 * `contentBase64` (the old inline mode) is rejected with a 400 that says what
 * to do instead, rather than ignored — a caller that believes it uploaded a
 * file when it did not is worse off than one that gets a clear error.
 */
import { randomUUID } from "node:crypto";
import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import { DynamoDBClient, GetItemCommand, PutItemCommand, UpdateItemCommand } from "@aws-sdk/client-dynamodb";
import { consumeDailyDocument, dailyLimitResponse } from "./daily-doc-cap.mjs";
import {
  parseBody,
  normalizeMetadata,
  parseExtractionMode,
  parseAiResponseFormat,
  parseDocumentTypes,
  classificationModeFor,
  rawKeyFor,
  serializeDocumentCounts,
} from "./body-parser.mjs";

const s3 = new S3Client({});
const ddb = new DynamoDBClient({});
const BUCKET = process.env.DOCUMENT_BUCKET;
const APPS_TABLE = process.env.APPS_TABLE;
const APP_USAGE_TABLE = process.env.APP_USAGE_TABLE;
const DOCUMENT_TABLE = process.env.DOCUMENT_TABLE;
const ALLOW_AI_RESPONSE_FORMAT_OVERRIDE =
  String(process.env.ALLOW_AI_RESPONSE_FORMAT_OVERRIDE || "").toLowerCase() === "true";
const ALLOWED_EXTENSIONS = new Set(["jpg", "jpeg", "png", "pdf", "xml", "xlsx", "csv"]);
// Extensions with no vision-based auto-classification at all: the enrichment
// pipeline's xml/tabular extractors require a declared type to even parse the
// file (see pipeline/xml_document.py, pipeline/tabular_document.py). Rejecting
// an undeclared upload here — rather than letting it reach Step Functions and
// fail deep inside model-invoke-lambda — also matters for cost: Bedrock
// Converse already treats "csv" as a natively supported document format, so
// an undeclared .csv would otherwise reach a REAL (billed) classification
// call before failing, unlike xml/xlsx which fail for free.
const REQUIRES_DECLARED_TYPE = new Set(["xml", "xlsx", "csv"]);
// How long a presigned upload URL stays valid. Generous enough for a slow
// connection uploading a large file without leaving it usable indefinitely.
const PRESIGN_EXPIRY_SECONDS = 15 * 60;

// La forma de cada tipo documental (UPPER_SNAKE_CASE) y el corte por comas
// viven en `parseDocumentTypes` (body-parser.mjs), un modulo puro y testeable.
// Ningun tipo se valida contra el catalogo aqui, para no acoplar el upload al
// catalogo; si un tipo no existe, el enrichment lo tratara como OTRO.

/**
 * Extract file extension from filename (lowercase).
 */
function getExtension(fileName) {
  const parts = fileName.split(".");
  return parts.length > 1 ? parts.pop().toLowerCase() : "";
}

/**
 * Build a standardized error response.
 */
function errorResponse(statusCode, error, message) {
  return {
    statusCode,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ error, message }),
  };
}

/**
 * Validate `documentType` (optional manual classification, one type or several
 * comma-separated) and `fileName` (required extension allow-list), shared by
 * both upload modes. Returns
 * { manualTypes, typeCounts, occurrenceMode, ext } on success, or an error
 * response to return as-is.
 */
function validateCommon(fileName, documentType, occurrenceMode) {
  const parsed = parseDocumentTypes(documentType, occurrenceMode);
  if (parsed.error) {
    return { error: errorResponse(400, parsed.error.code, parsed.error.message) };
  }
  const manualTypes = parsed.types;
  const typeCounts = parsed.counts;
  const resolvedOccurrenceMode = parsed.occurrenceMode;

  const ext = getExtension(fileName);
  if (!ext || !ALLOWED_EXTENSIONS.has(ext)) {
    return {
      error: errorResponse(
        400,
        "INVALID_FORMAT",
        `File format '${ext || "unknown"}' is not supported. Allowed: jpg, jpeg, png, pdf, xml, xlsx, csv`
      ),
    };
  }

  if (REQUIRES_DECLARED_TYPE.has(ext) && !manualTypes.length) {
    return {
      error: errorResponse(
        400,
        "DOCUMENT_TYPE_REQUIRED",
        `Field 'documentType' is required for .${ext} uploads — there is no ` +
          "vision-based auto-classification for structured data sources"
      ),
    };
  }

  // A list only makes sense when something has to CHOOSE among the types, and
  // for an already-structured source nothing does: xml/tabular extraction
  // resolves each field by xpath/cell against the one declared schema, with no
  // classification step to narrow. Rejecting it here rather than letting the
  // pipeline pick the first type keeps the caller from believing a file was
  // searched for several schemas when it was only ever read as one.
  if (REQUIRES_DECLARED_TYPE.has(ext) && manualTypes.length > 1) {
    return {
      error: errorResponse(
        400,
        "INVALID_DOCUMENT_TYPE",
        `Field 'documentType' must name exactly one type for .${ext} uploads — ` +
          "there is no vision-based auto-classification for structured data " +
          "sources, so there is nothing to choose between several types"
      ),
    };
  }

  return { manualTypes, typeCounts, occurrenceMode: resolvedOccurrenceMode, ext };
}

/**
 * Resolve the x-api-key used for this request (API Gateway hands its
 * ApiKeyId back on `event.requestContext.identity.apiKeyId` whenever the
 * method has `apiKeyRequired: true`) to the human-readable app name recorded
 * by `pnpm apps:register` (scripts/manage-apps.mjs), plus the app's
 * documents-per-day cap (`MaxDocsPerDay`, null = no cap). Fails open — a
 * lookup problem must never block an upload — and distinguishes "no key on
 * this request" (appOrigin null, e.g. dev with the key requirement off) from
 * "a valid key API Gateway accepted, but nobody registered it here"
 * ("unregistered", e.g. the legacy idp-default key before `pnpm apps:adopt`).
 */
async function resolveApp(apiKeyId) {
  if (!apiKeyId || !APPS_TABLE) return {
    appOrigin: null, maxDocsPerDay: null, allowAiResponseFormatOverride: false,
  };
  try {
    const { Item } = await ddb.send(
      new GetItemCommand({
        TableName: APPS_TABLE,
        Key: { ApiKeyId: { S: apiKeyId } },
      })
    );
    return {
      appOrigin: Item?.AppName?.S || "unregistered",
      maxDocsPerDay: Item?.MaxDocsPerDay?.N ?? null,
      allowAiResponseFormatOverride: Item?.AllowAiResponseFormatOverride?.BOOL === true,
    };
  } catch (err) {
    console.error("Failed to resolve appOrigin:", err);
    return {
      appOrigin: "unregistered", maxDocsPerDay: null,
      allowAiResponseFormatOverride: false,
    };
  }
}

/**
 * Lambda handler.
 */
export async function handler(event) {
  const parsed = parseBody(event);
  if (parsed.error) return errorResponse(400, parsed.error.code, parsed.error.message);
  const body = parsed.body;

  const { fileName, contentBase64, metadata, documentType, modelId } = body;
  // camelCase is canonical like the rest of this API; accept the snake_case
  // spelling too as a compatibility alias.
  if (
    body.occurrenceMode !== undefined &&
    body.occurrence_mode !== undefined &&
    String(body.occurrenceMode).trim().toLowerCase() !==
      String(body.occurrence_mode).trim().toLowerCase()
  ) {
    return errorResponse(
      400,
      "INVALID_OCCURRENCE_MODE",
      "Fields 'occurrenceMode' and 'occurrence_mode' disagree; send only occurrenceMode"
    );
  }
  const requestedOccurrenceMode = body.occurrenceMode ?? body.occurrence_mode;

  const extraction = parseExtractionMode(body.extractionMode);
  if (extraction.error) {
    return errorResponse(400, extraction.error.code, extraction.error.message);
  }
  const aiResponse = parseAiResponseFormat(body.aiResponseFormat);
  if (aiResponse.error) {
    return errorResponse(400, aiResponse.error.code, aiResponse.error.message);
  }

  if (!fileName) {
    return errorResponse(400, "MISSING_FIELD", "Field 'fileName' is required");
  }

  const validated = validateCommon(fileName, documentType, requestedOccurrenceMode);
  if (validated.error) return validated.error;
  const { manualTypes, typeCounts, occurrenceMode } = validated;   // `ext` ya lo validó validateCommon

  // Optional manual classification via documentType. The declared types travel
  // as ONE segment of the S3 key — that key is the only carrier of this signal,
  // read independently by the SQS consumer, the model-invoke Lambda and the
  // enrichment Lambda, so joining them here reaches all three with no new
  // channel.
  //   raw/{TYPE}/{uuid}/file        one type   -> honoured as-is, never re-segmented
  //   raw/{A,B}/{uuid}/file         several    -> segmented, but classified ONLY among A and B
  //   raw/{uuid}/file               none       -> Bedrock auto-classifies from content
  const documentId = randomUUID();
  const key = rawKeyFor(manualTypes, documentId, fileName);

  // Which app made this request, resolved from its x-api-key (present only
  // when the method requires one — see resolveApp above).
  const apiKeyId = event.requestContext?.identity?.apiKeyId;
  const { appOrigin, maxDocsPerDay, allowAiResponseFormatOverride } = await resolveApp(apiKeyId);
  if (
    aiResponse.format &&
    !ALLOW_AI_RESPONSE_FORMAT_OVERRIDE &&
    !allowAiResponseFormatOverride
  ) {
    return errorResponse(
      403,
      "AI_RESPONSE_FORMAT_OVERRIDE_FORBIDDEN",
      "This API key is not allowed to override aiResponseFormat"
    );
  }

  // The caller's own `metadata` rides along as S3 object metadata, normalized
  // to what S3 can actually hold (see normalizeMetadata) — that is also what
  // makes it searchable later: the SQS consumer reads it back off the object
  // and it ends up on the result envelope as `metadata`, filterable with
  // `GET /documents?meta.<clave>=<valor>`.
  const normalizedMetadata = normalizeMetadata(metadata);
  if (normalizedMetadata.error) {
    return errorResponse(400, normalizedMetadata.error.code, normalizedMetadata.error.message);
  }
  const objectMetadata = { ...normalizedMetadata.metadata };
  // A per-request model override travels as S3 object metadata. The
  // enrichment Lambda only honours it when the deployment allows it (dev), so
  // accepting it here is inert in prod. The execution keys below are set AFTER the
  // caller's metadata and are stripped from it by normalizeMetadata, so a body
  // field can never impersonate another app or smuggle in a model override.
  if (typeof modelId === "string" && modelId.trim()) {
    objectMetadata["model-id"] = modelId.trim();
  }
  if (extraction.mode !== "catalog") {
    objectMetadata["extraction-mode"] = extraction.mode;
  }
  if (aiResponse.format) {
    objectMetadata["ai-response-format"] = aiResponse.format;
  }
  if (appOrigin) {
    objectMetadata["app-origin"] = appOrigin;
  }
  // Cuántos documentos de cada tipo buscar. Va por el mismo canal que
  // `model-id`: el enrichment lo lee del objeto con `head_object`/`get_object`
  // (`handler._fetch_document` ya lee `Metadata`), así que no hay que tocar ni
  // la llave ni el consumidor de SQS. Se escribe DESPUÉS de la metadata del
  // caller y `normalizeMetadata` ya la despojó de esta clave reservada, así que
  // nadie puede pedir un tipo y declarar otro número por su cuenta.
  const declaredCounts = serializeDocumentCounts(typeCounts);
  if (declaredCounts) {
    objectMetadata["document-counts"] = declaredCounts;
  }

  if (contentBase64 !== undefined && contentBase64 !== null) {
    return errorResponse(
      400,
      "INLINE_UPLOAD_REMOVED",
      "Inline uploads are no longer supported. Omit 'contentBase64': this endpoint " +
        "returns an 'uploadUrl' and you PUT the file straight to it. Same two steps " +
        "for every file size."
    );
  }

  // Documents-per-day cap (daily-doc-cap.mjs): counted only here and in
  // reprocess, after every validation above so a rejected request never
  // spends the budget. Status polling (GET) never reaches this.
  const cap = await consumeDailyDocument({
    update: (params) => ddb.send(new UpdateItemCommand(params)),
    table: APP_USAGE_TABLE,
    apiKeyId,
    appName: appOrigin,
    maxDocsPerDay,
  });
  if (!cap.allowed) return dailyLimitResponse(cap);

  return handlePresigned(
    documentId, key, manualTypes, objectMetadata, appOrigin, typeCounts, occurrenceMode
  );
}

/**
 * Presigned mode: no bytes touch this Lambda. Returns a short-lived S3 PUT
 * URL for the caller to upload directly to.
 *
 * The caller sends NO extra headers on the PUT. SigV4 presigning hoists
 * `x-amz-*` headers into the query string, so the object metadata already
 * travels inside the URL and is covered by the signature
 * (`X-Amz-SignedHeaders=host`). Sending it as headers too makes S3 reject the
 * upload with `403 AccessDenied: There were headers present in the request
 * which were not signed` — verified against the real bucket.
 *
 * Content-Type is deliberately not bound either: the enrichment pipeline
 * infers the file type from the key's extension and never reads S3's stored
 * Content-Type, so leaving it unset keeps the PUT usable from any client with
 * zero header bookkeeping.
 */
async function handlePresigned(
  documentId, key, manualTypes = [], objectMetadata = {}, appOrigin = null,
  typeCounts = {}, occurrenceMode = null
) {
  // El string unido es lo que viaja: en la key de S3, en la columna
  // RequestedDocumentType y en la respuesta. "" cuando no se declaro ninguno.
  const declaredTypes = manualTypes.join(",");
  let uploadUrl;
  const registeredAt = new Date().toISOString();
  const uploadExpiresAt = new Date(
    Date.parse(registeredAt) + PRESIGN_EXPIRY_SECONDS * 1000
  ).toISOString();
  try {
    uploadUrl = await getSignedUrl(
      s3,
      new PutObjectCommand({
        Bucket: BUCKET,
        Key: key,
        // Only bound into the signature when there is metadata to carry:
        // binding it unconditionally would force every caller to echo the
        // matching headers, which is exactly what this path avoids.
        ...(Object.keys(objectMetadata).length ? { Metadata: objectMetadata } : {}),
      }),
      { expiresIn: PRESIGN_EXPIRY_SECONDS }
    );
  } catch (err) {
    console.error("Failed to generate presigned URL:", err);
    return errorResponse(500, "PRESIGN_FAILED", "Failed to generate upload URL");
  }

  // Reserve the id before returning it. This closes the old ambiguity where
  // GET /documents/{id} returned 404 both for an unknown id and for a valid
  // upload that had not reached Step Functions yet. The workflow later
  // replaces this lightweight row with its normal tracking row.
  try {
    if (!DOCUMENT_TABLE) throw new Error("DOCUMENT_TABLE is not configured");
    await ddb.send(
      new PutItemCommand({
        TableName: DOCUMENT_TABLE,
        Item: {
          DocumentId: { S: documentId },
          ProcessingStatus: { S: "AWAITING_UPLOAD" },
          ProcessingStatusUpdatedAt: { S: registeredAt },
          RegisteredAt: { S: registeredAt },
          UploadExpiresAt: { S: uploadExpiresAt },
          UploadKey: { S: key },
          JobOwner: { S: appOrigin || "dev-unrestricted" },
          ...(declaredTypes ? { RequestedDocumentType: { S: declaredTypes } } : {}),
        },
        ConditionExpression: "attribute_not_exists(DocumentId)",
      })
    );
  } catch (err) {
    console.error("Failed to register pending document:", err);
    return errorResponse(
      500,
      "JOB_REGISTRATION_FAILED",
      "Failed to register document processing status"
    );
  }

  return {
    statusCode: 200,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      documentId,
      bucket: BUCKET,
      key,
      documentType: declaredTypes || null,
      occurrenceMode,
      // Decisiones explícitas del request: sufijos y/o occurrenceMode. Si el
      // modo se omitió, los defaults de catálogo se resuelven en enrichment.
      ...(Object.keys(typeCounts).length ? { documentTypeCounts: typeCounts } : {}),
      // "guided": el archivo SI se segmenta y clasifica, pero el clasificador
      // solo puede elegir entre los tipos declarados (lo demás se omite de la salida).
      // "manual" sigue significando exactamente lo de siempre: un solo tipo,
      // honrado tal cual, sin re-segmentar.
      classificationMode: classificationModeFor(manualTypes),
      uploadUrl,
      uploadMethod: "PUT",
      // NOTA: deliberadamente NO se devuelve una lista de cabeceras que el
      // cliente deba reenviar. SigV4 **hoistea** los `x-amz-*` al query string
      // al prefirmar, así que la metadata ya viaja DENTRO de la URL y queda
      // cubierta por la firma (`X-Amz-SignedHeaders=host`). Mandarlos además
      // como headers hace que S3 rechace el PUT con
      // `403 AccessDenied: There were headers present in the request which
      // were not signed` — verificado contra el bucket real. El PUT correcto
      // no lleva ninguna cabecera extra.
      uploadExpiresInSeconds: PRESIGN_EXPIRY_SECONDS,
      // Absolute deadline alongside the relative one: a client that queues the
      // upload can tell whether the URL is still good without having to know
      // when it asked for it.
      uploadExpiresAt,
      uploadedAt: registeredAt,
      registeredAt,
      processingStatus: "AWAITING_UPLOAD",
      statusUrl: `/documents/${documentId}`,
      retryAfterSeconds: 5,
    }),
  };
}
