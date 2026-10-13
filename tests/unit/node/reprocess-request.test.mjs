import assert from "node:assert/strict";
import test from "node:test";

import {
  classificationModeFor,
  aiResponseFormatOf,
  deserializeDocumentCounts,
  inheritedDocumentType,
  occurrenceModeOf,
  parseBody,
  parseDocumentTypes as parseReprocessTypes,
  rawKeyFor,
  serializeDocumentCounts,
  userMetadataForReprocess,
  validateSourceFile,
} from "../../../src/document-processing/resources/query-lambda/reprocess-request.mjs";
import {
  parseDocumentTypes as parseUploadTypes,
} from "../../../src/document-processing/resources/upload-lambda/body-parser.mjs";

test("reprocess and upload accept exactly the same documentType grammar", () => {
  const cases = [
    [undefined, undefined],
    ["PEDIMENTO_ERP", undefined],
    ["PEDIMENTO_ERP,FACTURA_FLETE_EXTRANJERO", undefined],
    ["A:2,B:all", undefined],
    ["A,B", "single"],
    ["A,B", "multiple"],
    ["A:0", undefined],
    [["A", "B"], undefined],
    ["lowercase", undefined],
  ];
  for (const [documentType, occurrenceMode] of cases) {
    assert.deepEqual(
      parseReprocessTypes(documentType, occurrenceMode),
      parseUploadTypes(documentType, occurrenceMode),
      `${JSON.stringify(documentType)} / ${JSON.stringify(occurrenceMode)}`
    );
  }
});

test("omitting documentType inherits it from the old job row", () => {
  assert.equal(
    inheritedDocumentType({ RequestedDocumentType: "PEDIMENTO_ERP,FACTURA_FLETE_EXTRANJERO" }),
    "PEDIMENTO_ERP,FACTURA_FLETE_EXTRANJERO"
  );
  assert.equal(
    inheritedDocumentType({ UploadKey: "raw/PEDIMENTO_ERP/old-id/file.pdf" }),
    "PEDIMENTO_ERP"
  );
  assert.equal(
    inheritedDocumentType({ SourceKey: "source/PEDIMENTO_ERP/old-id/file.pdf" }),
    "PEDIMENTO_ERP"
  );
  assert.equal(inheritedDocumentType({ UploadKey: "raw/old-id/file.pdf" }), "");
  assert.equal(inheritedDocumentType({ RequestedDocumentType: "AUTO" }), "");
});

test("an explicit null documentType requests auto classification", () => {
  assert.deepEqual(parseReprocessTypes(null).types, []);
  assert.equal(classificationModeFor([]), "auto");
  assert.equal(rawKeyFor([], "new-id", "file.pdf"), "source/new-id/file.pdf");
});

test("multiple applies to all target types and counts travel as S3 metadata", () => {
  const parsed = parseReprocessTypes("PEDIMENTO_ERP,FACTURA_FLETE_EXTRANJERO", "multiple");
  assert.deepEqual(parsed.counts, {
    PEDIMENTO_ERP: "all",
    FACTURA_FLETE_EXTRANJERO: "all",
  });
  const encoded = serializeDocumentCounts(parsed.counts);
  assert.equal(encoded, "PEDIMENTO_ERP=all,FACTURA_FLETE_EXTRANJERO=all");
  assert.deepEqual(deserializeDocumentCounts(encoded, parsed.types), parsed.counts);
});

test("inherited count metadata ignores unknown or malformed entries", () => {
  assert.deepEqual(
    deserializeDocumentCounts("A=2,B=all,C=0,X=9,bad", ["A", "B", "C"]),
    { A: 2, B: "all" }
  );
});

test("structured sources require exactly one declared schema", () => {
  assert.equal(validateSourceFile("data.xml", []).error.code, "DOCUMENT_TYPE_REQUIRED");
  assert.equal(validateSourceFile("data.xlsx", ["A", "B"]).error.code, "INVALID_DOCUMENT_TYPE");
  assert.deepEqual(validateSourceFile("data.csv", ["A"]), { ext: "csv" });
  assert.deepEqual(validateSourceFile("bundle.pdf", ["A", "B"]), { ext: "pdf" });
});

test("request body supports JSON, form and occurrence_mode alias", () => {
  assert.deepEqual(
    parseBody({ body: '{"documentType":"A,B","occurrenceMode":"multiple"}' }).body,
    { documentType: "A,B", occurrenceMode: "multiple" }
  );
  assert.deepEqual(
    parseBody({
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: "documentType=A%2CB&occurrence_mode=single",
    }).body,
    { documentType: "A,B", occurrence_mode: "single" }
  );
  assert.equal(occurrenceModeOf({ occurrence_mode: "multiple" }).occurrenceMode, "multiple");
  assert.equal(
    occurrenceModeOf({ occurrenceMode: "single", occurrence_mode: "multiple" }).error.code,
    "INVALID_OCCURRENCE_MODE"
  );
});

test("reprocess valida el mismo override compacto o legacy", () => {
  assert.deepEqual(aiResponseFormatOf({}), { format: null });
  assert.deepEqual(aiResponseFormatOf({ aiResponseFormat: "COMPACT" }), { format: "compact" });
  assert.equal(aiResponseFormatOf({ aiResponseFormat: "otro" }).error.code, "INVALID_AI_RESPONSE_FORMAT");
});

test("current DynamoDB metadata wins and an authoritative empty map preserves deletions", () => {
  const source = {
    lote: "OLD",
    borrada: "X",
    "app-origin": "ERP",
    "document-counts": "A=all",
  };
  assert.deepEqual(
    userMetadataForReprocess(source, { lote: "CURRENT" }, true),
    { lote: "CURRENT" }
  );
  assert.deepEqual(userMetadataForReprocess(source, {}, true), {});
  assert.deepEqual(
    userMetadataForReprocess(source, { lote: "CURRENT" }, false),
    { lote: "CURRENT", borrada: "X" }
  );
});
