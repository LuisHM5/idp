import { test } from "node:test";
import assert from "node:assert/strict";

import {
  canSeePendingDocument,
  isPendingProcessingStatus,
  processingStatusFor,
} from "../../../src/document-processing/resources/query-lambda/processing-status.mjs";

test("expone el ciclo técnico estable sin filtrar estados internos del blueprint", () => {
  assert.equal(processingStatusFor({ ProcessingStatus: "AWAITING_UPLOAD" }), "AWAITING_UPLOAD");
  assert.equal(processingStatusFor({ ProcessingStatus: "QUEUED" }), "QUEUED");
  assert.equal(processingStatusFor({ WorkflowStatus: "classification-complete" }), "PROCESSING");
  assert.equal(processingStatusFor({ WorkflowStatus: "complete" }), "FINISHED");
  assert.equal(processingStatusFor({ WorkflowStatus: "enrichment-failure" }), "FAILED");
  assert.equal(processingStatusFor({ ProcessingStatus: "PROCESSING" }, true), "FINISHED");
});

test("solo los estados no terminales piden otro GET", () => {
  for (const status of ["AWAITING_UPLOAD", "QUEUED", "PROCESSING"]) {
    assert.equal(isPendingProcessingStatus(status), true);
  }
  assert.equal(isPendingProcessingStatus("FINISHED"), false);
  assert.equal(isPendingProcessingStatus("FAILED"), false);
});

test("la fila ligera respeta propietario y tipo declarado", () => {
  const own = {
    appName: "CEDIS",
    accessScope: "own",
    allowedDocTypes: ["FACTURA_FLETE_EXTRANJERO"],
  };
  assert.equal(canSeePendingDocument(own, {
    JobOwner: "CEDIS",
    RequestedDocumentType: "FACTURA_FLETE_EXTRANJERO",
  }), true);
  assert.equal(canSeePendingDocument(own, {
    JobOwner: "OTRA_APP",
    RequestedDocumentType: "FACTURA_FLETE_EXTRANJERO",
  }), false);
  assert.equal(canSeePendingDocument(own, {
    JobOwner: "CEDIS",
    RequestedDocumentType: "OTRO_TIPO",
  }), false);
  assert.equal(canSeePendingDocument(own, {
    JobOwner: "CEDIS",
    RequestedDocumentType: "AUTO",
  }), true);
});

