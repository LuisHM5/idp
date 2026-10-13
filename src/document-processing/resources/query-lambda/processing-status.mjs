export const PROCESSING_STATUSES = Object.freeze({
  AWAITING_UPLOAD: "AWAITING_UPLOAD",
  QUEUED: "QUEUED",
  PROCESSING: "PROCESSING",
  FINISHED: "FINISHED",
  FAILED: "FAILED",
});

const FAILURE_WORKFLOW_STATUSES = new Set([
  "classification-failure",
  "processing-failure",
  "enrichment-failure",
  "post-processing-failure",
]);

/**
 * Stable public lifecycle, deliberately separate from the state machine's
 * implementation-specific WorkflowStatus values.
 */
export function processingStatusFor(item, hasResult = false) {
  if (hasResult || item?.WorkflowStatus === "complete") {
    return PROCESSING_STATUSES.FINISHED;
  }
  if (FAILURE_WORKFLOW_STATUSES.has(item?.WorkflowStatus)) {
    return PROCESSING_STATUSES.FAILED;
  }
  if (Object.values(PROCESSING_STATUSES).includes(item?.ProcessingStatus)) {
    return item.ProcessingStatus;
  }
  // A legacy row already created by Step Functions but without the new public
  // attribute is necessarily past upload and queueing.
  if (item?.WorkflowStatus) return PROCESSING_STATUSES.PROCESSING;
  return PROCESSING_STATUSES.AWAITING_UPLOAD;
}

export function isPendingProcessingStatus(status) {
  return status === PROCESSING_STATUSES.AWAITING_UPLOAD ||
    status === PROCESSING_STATUSES.QUEUED ||
    status === PROCESSING_STATUSES.PROCESSING;
}

/** Authorize a lightweight row before an extraction envelope exists. */
export function canSeePendingDocument(caller, item) {
  if (caller.accessScope === "own" && item.JobOwner !== caller.appName) {
    return false;
  }
  // `RequestedDocumentType` carries what the uploader declared: one type, or
  // several comma-separated (`POST /documents` accepts a list so the pipeline
  // classifies among exactly those). `"AUTO"` is the sentinel for "nothing
  // declared", written by the SQS consumer when the key has no type segment.
  const requestedTypes = item.RequestedDocumentType === "AUTO"
    ? []
    : String(item.RequestedDocumentType || "")
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean);
  // EVERY declared type has to be allowed, not just one of them: a caller must
  // not be able to reach a type outside its grant by listing it alongside one
  // that is inside. Splitting also matters for the simple case — without it a
  // scoped app could not even poll its own multi-type upload, because "A,B"
  // never appears verbatim in `allowedDocTypes`.
  if (
    requestedTypes.length &&
    caller.allowedDocTypes.length &&
    !requestedTypes.every((t) => caller.allowedDocTypes.includes(t))
  ) {
    return false;
  }
  return true;
}
