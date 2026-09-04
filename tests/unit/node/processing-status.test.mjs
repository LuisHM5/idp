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


test("la fila ligera entiende varios tipos declarados", () => {
  const caller = {
    appName: "CEDIS",
    accessScope: "own",
    allowedDocTypes: ["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"],
  };
  // Sin partir por coma esto daba false: "A,B" nunca aparece literal en
  // allowedDocTypes, así que una app con scope no podía ni consultar el
  // estado de su PROPIA subida multi-tipo.
  assert.equal(canSeePendingDocument(caller, {
    JobOwner: "CEDIS",
    RequestedDocumentType: "PEDIMENTO_ADUANAL,FACTURA_HONORARIOS",
  }), true);
  // TODOS los tipos tienen que estar permitidos, no basta con uno: si no,
  // listar un tipo prohibido junto a uno permitido sería la forma de colarlo.
  assert.equal(canSeePendingDocument(caller, {
    JobOwner: "CEDIS",
    RequestedDocumentType: "PEDIMENTO_ADUANAL,TIPO_PROHIBIDO",
  }), false);
  // Una app sin restricción de tipos ve cualquier lista.
  assert.equal(canSeePendingDocument(
    { appName: "CEDIS", accessScope: "own", allowedDocTypes: [] },
    { JobOwner: "CEDIS", RequestedDocumentType: "PEDIMENTO_ADUANAL,TIPO_PROHIBIDO" },
  ), true);
});
