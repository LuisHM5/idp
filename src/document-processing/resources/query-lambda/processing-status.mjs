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
 * Stable public lifecycle, deliberately separate from the blueprint's
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
  const requestedType = item.RequestedDocumentType === "AUTO"
    ? null
    : item.RequestedDocumentType;
  if (
    requestedType &&
    caller.allowedDocTypes.length &&
    !caller.allowedDocTypes.includes(requestedType)
  ) {
    return false;
  }
  return true;
}
