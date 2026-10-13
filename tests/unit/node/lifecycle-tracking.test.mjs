import { test } from "node:test";
import assert from "node:assert/strict";

import { applyLifecycleTracking } from "../../../src/document-processing/resources/query-lambda/lifecycle-tracking.mjs";

test("calcula upload hasta FINISHED sin cambiar processingSeconds", () => {
  const result = applyLifecycleTracking(
    { tracking: { processingSeconds: 162.47, uploadToResultSeconds: 174.52 } },
    {
      UploadedAt: "2026-10-12T21:02:40.000Z",
      ProcessingCompletedAt: "2026-10-12T21:05:36.250Z",
    },
  );

  assert.equal(result.tracking.processingSeconds, 162.47);
  assert.equal(result.tracking.uploadToResultSeconds, 174.52);
  assert.equal(result.tracking.workflowCompletedAt, "2026-10-12T21:05:36.250Z");
  assert.equal(result.tracking.uploadToCompletionSeconds, 176.25);
  assert.equal(result.tracking.uploadToCompletionMs, 176250);
});

test("sin evento terminal conserva sólo el instante de upload", () => {
  const original = { documentId: "doc", tracking: {} };
  const result = applyLifecycleTracking(original, { UploadedAt: "2026-10-12T21:02:40Z" });
  assert.equal(result.tracking.uploadedAt, "2026-10-12T21:02:40Z");
  assert.equal(result.tracking.uploadToCompletionSeconds, undefined);
});

test("timestamps inválidos nunca fabrican una duración", () => {
  const result = applyLifecycleTracking(
    { tracking: {} },
    { UploadedAt: "bad", ProcessingCompletedAt: "also-bad" },
  );
  assert.equal(result.tracking.uploadToCompletionSeconds, undefined);
});
