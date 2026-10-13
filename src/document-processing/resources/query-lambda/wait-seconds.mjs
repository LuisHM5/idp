/** Parse the bounded long-poll duration accepted by GET /documents/{id}. */
export function parseWaitSeconds(value) {
  if (value === undefined || value === null || value === "") return { seconds: 0 };
  const raw = String(value).trim();
  if (!/^\d+(?:\.\d+)?$/.test(raw)) {
    return { error: "waitSeconds must be a number between 0 and 20" };
  }
  const seconds = Number(raw);
  if (!Number.isFinite(seconds) || seconds < 0 || seconds > 20) {
    return { error: "waitSeconds must be a number between 0 and 20" };
  }
  return { seconds };
}
