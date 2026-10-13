/** Overlay timestamps only known after the workflow terminal event.
 *
 * Enrichment can measure upload -> result construction, but the final
 * CopyToProcessed/EventBridge transition happens afterwards. The query API has
 * both S3's UploadedAt and the terminal ProcessingCompletedAt columns, so it
 * can expose the true upload -> FINISHED/FAILED wall clock without rewriting
 * the immutable EnrichmentResult snapshot.
 */
export function applyLifecycleTracking(envelope, item = {}) {
  if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) return envelope;
  const current = envelope.tracking && typeof envelope.tracking === "object"
    ? envelope.tracking
    : {};
  const uploadedAt = item.UploadedAt || current.uploadedAt;
  const workflowCompletedAt = item.ProcessingCompletedAt;
  if (!uploadedAt && !workflowCompletedAt) return envelope;

  const tracking = { ...current };
  if (uploadedAt) tracking.uploadedAt = uploadedAt;
  if (workflowCompletedAt) tracking.workflowCompletedAt = workflowCompletedAt;

  const uploadedMs = Date.parse(uploadedAt || "");
  const completedMs = Date.parse(workflowCompletedAt || "");
  if (Number.isFinite(uploadedMs) && Number.isFinite(completedMs)) {
    const elapsedMs = Math.max(0, completedMs - uploadedMs);
    tracking.uploadToCompletionMs = Math.round(elapsedMs * 100) / 100;
    tracking.uploadToCompletionSeconds = Math.round(elapsedMs / 10) / 100;
  }
  return { ...envelope, tracking };
}
