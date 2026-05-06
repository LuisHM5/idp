/**
 * UploadLambda - Document upload handler.
 *
 * Accepts documents via API Gateway (base64-encoded JSON body),
 * validates format, generates a unique document ID, stores in S3,
 * and returns the document reference.
 */

import { randomUUID } from "node:crypto";
import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";

const s3 = new S3Client({});
const BUCKET = process.env.DOCUMENT_BUCKET;
const ALLOWED_EXTENSIONS = new Set(["jpg", "jpeg", "png", "pdf"]);

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

  const { fileName, contentBase64, metadata } = body;

  // Validate required fields
  if (!fileName) {
    return errorResponse(400, "MISSING_FIELD", "Field 'fileName' is required");
  }
  if (!contentBase64) {
    return errorResponse(
      400,
      "MISSING_FIELD",
      "Field 'contentBase64' is required"
    );
  }

  // Validate file extension
  const ext = getExtension(fileName);
  if (!ext || !ALLOWED_EXTENSIONS.has(ext)) {
    return errorResponse(
      400,
      "INVALID_FORMAT",
      `File format '${ext || "unknown"}' is not supported. Allowed: jpg, jpeg, png, pdf`
    );
  }

  // Validate base64 content is non-empty
  if (contentBase64.trim().length === 0) {
    return errorResponse(400, "EMPTY_CONTENT", "File content cannot be empty");
  }

  // Decode base64
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

  // Generate document ID and S3 key
  const documentId = randomUUID();
  const key = `raw/${documentId}/${fileName}`;

  // Upload to S3
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
  };
  return mimeMap[ext] || "application/octet-stream";
}
