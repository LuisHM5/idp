/**
 * UploadLambda - Document upload handler.
 *
 * Two modes, same endpoint (POST /upload):
 * - Inline (small files): body carries `contentBase64` — the Lambda decodes
 *   and PUTs to S3 itself, same as before. Bounded by API Gateway's 10MB
 *   request cap and Lambda's 6MB synchronous-invoke payload cap (hard AWS
 *   limits, not configurable) — base64's ~33% overhead means this is only
 *   safe up to a few MB of original file size.
 * - Presigned (large files): body OMITS `contentBase64`. The Lambda
 *   generates the documentId/S3 key and returns a short-lived presigned S3
 *   PUT URL instead of touching the bytes at all. The caller then PUTs the
 *   raw file directly to that URL — this bypasses API Gateway and Lambda
 *   entirely for the file content, so there is no practical size ceiling
 *   (S3 PUT supports up to 5GB), and it works for any HTTP client, not just
 *   ones with AWS credentials.
 */

import { randomUUID } from "node:crypto";
import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";

const s3 = new S3Client({});
const BUCKET = process.env.DOCUMENT_BUCKET;
const ALLOWED_EXTENSIONS = new Set(["jpg", "jpeg", "png", "pdf", "xml"]);
// How long a presigned upload URL stays valid. Generous enough for a slow
// connection uploading a large file without leaving it usable indefinitely.
const PRESIGN_EXPIRY_SECONDS = 15 * 60;

// Tipo documental manual: UPPER_SNAKE_CASE. Debe empezar con letra.
// No se valida contra el catalogo aqui para no acoplar el upload al catalogo;
// si el tipo no existe en el catalogo, el enrichment lo tratara como OTRO.
const DOCUMENT_TYPE_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/;

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
 * Validate `documentType` (optional manual classification) and `fileName`
 * (required extension allow-list), shared by both upload modes.
 * Returns { manualType, ext } on success, or an error response to return
 * as-is.
 */
function validateCommon(fileName, documentType) {
  let manualType = null;
  if (documentType !== undefined && documentType !== null && documentType !== "") {
    if (typeof documentType !== "string" || !DOCUMENT_TYPE_PATTERN.test(documentType)) {
      return {
        error: errorResponse(
          400,
          "INVALID_DOCUMENT_TYPE",
          "Field 'documentType' must match ^[A-Z][A-Z0-9_]{0,63}$ (UPPER_SNAKE_CASE)"
        ),
      };
    }
    manualType = documentType;
  }

  const ext = getExtension(fileName);
  if (!ext || !ALLOWED_EXTENSIONS.has(ext)) {
    return {
      error: errorResponse(
        400,
        "INVALID_FORMAT",
        `File format '${ext || "unknown"}' is not supported. Allowed: jpg, jpeg, png, pdf, xml`
      ),
    };
  }

  return { manualType, ext };
}

/**
 * Lambda handler.
 */
export async function handler(event) {
  let body;
  try {
    body =
      typeof event.body === "string" ? JSON.parse(event.body) : event.body || {};
  } catch {
    return errorResponse(400, "INVALID_JSON", "Request body is not valid JSON");
  }

  const { fileName, contentBase64, metadata, documentType, modelId } = body;

  if (!fileName) {
    return errorResponse(400, "MISSING_FIELD", "Field 'fileName' is required");
  }

  const validated = validateCommon(fileName, documentType);
  if (validated.error) return validated.error;
  const { manualType, ext } = validated;

  // Optional manual classification via documentType.
  // If provided, the document is placed under raw/{TYPE}/{uuid}/ and the
  // classification step short-circuits without calling Bedrock.
  // If omitted, Bedrock auto-classifies from content.
  const documentId = randomUUID();
  const key = manualType
    ? `raw/${manualType}/${documentId}/${fileName}`
    : `raw/${documentId}/${fileName}`;

  // A per-request model override travels as S3 object metadata. The
  // enrichment Lambda only honours it when the deployment allows it (dev), so
  // accepting it here is inert in prod.
  const objectMetadata = { ...(metadata || {}) };
  if (typeof modelId === "string" && modelId.trim()) {
    objectMetadata["model-id"] = modelId.trim();
  }

  if (contentBase64 === undefined || contentBase64 === null) {
    return handlePresigned(documentId, key, manualType, objectMetadata);
  }
  return handleInline(documentId, key, manualType, ext, contentBase64, objectMetadata);
}

/**
 * Presigned mode: no bytes touch this Lambda. Returns a short-lived S3 PUT
 * URL for the caller to upload directly to.
 *
 * Deliberately does NOT bind Content-Type into the signature: doing so
 * would require every caller to send back the exact same header value or
 * get a SignatureDoesNotMatch error. The enrichment pipeline never trusts
 * S3's stored Content-Type anyway — it infers the file type from the key's
 * extension (see `_fetch_document` in the enrichment Lambda) — so leaving
 * it unset costs nothing and keeps the presigned PUT usable from any client
 * (`curl -T`, a browser `fetch`, etc.) without extra header bookkeeping.
 */
async function handlePresigned(documentId, key, manualType, objectMetadata = {}) {
  let uploadUrl;
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

  return {
    statusCode: 200,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      documentId,
      bucket: BUCKET,
      key,
      documentType: manualType,
      classificationMode: manualType ? "manual" : "auto",
      uploadUrl,
      uploadMethod: "PUT",
      // Headers the caller must echo on the PUT for the signature to match.
      ...(Object.keys(objectMetadata).length
        ? {
            uploadHeaders: Object.fromEntries(
              Object.entries(objectMetadata).map(([k, v]) => [`x-amz-meta-${k}`, v])
            ),
          }
        : {}),
      uploadExpiresInSeconds: PRESIGN_EXPIRY_SECONDS,
      uploadedAt: new Date().toISOString(),
    }),
  };
}

/**
 * Inline mode: decode contentBase64 here and PUT to S3 directly, same
 * behavior as before this endpoint also supported presigned uploads.
 */
async function handleInline(documentId, key, manualType, ext, contentBase64, metadata) {
  if (contentBase64.trim().length === 0) {
    return errorResponse(400, "EMPTY_CONTENT", "File content cannot be empty");
  }

  let fileBuffer;
  try {
    fileBuffer = Buffer.from(contentBase64, "base64");
    if (fileBuffer.length === 0) {
      return errorResponse(400, "EMPTY_CONTENT", "Decoded file content is empty");
    }
  } catch {
    return errorResponse(
      400,
      "INVALID_BASE64",
      "Field 'contentBase64' is not valid base64"
    );
  }

  try {
    await s3.send(
      new PutObjectCommand({
        Bucket: BUCKET,
        Key: key,
        Body: fileBuffer,
        ContentType: getMimeType(ext),
        Metadata: metadata || {},
      })
    );
  } catch (err) {
    console.error("S3 PutObject failed:", err);
    return errorResponse(500, "UPLOAD_FAILED", "Failed to store document in S3");
  }

  return {
    statusCode: 200,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      documentId,
      bucket: BUCKET,
      key,
      documentType: manualType,
      classificationMode: manualType ? "manual" : "auto",
      uploadedAt: new Date().toISOString(),
    }),
  };
}

function getMimeType(ext) {
  const mimeMap = {
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    png: "image/png",
    pdf: "application/pdf",
    xml: "application/xml",
  };
  return mimeMap[ext] || "application/octet-stream";
}
