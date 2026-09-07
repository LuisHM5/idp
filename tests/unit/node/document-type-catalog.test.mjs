import assert from "node:assert/strict";
import test from "node:test";

import {
  DOCUMENT_TYPES_PAGE_SIZE,
  findDocumentType,
  mergeDocumentTypes,
  paginateDocumentTypes,
  parseIncludeSchema,
  parsePage,
  scopeToAllowedDocTypes,
} from "../../../src/document-processing/resources/query-lambda/document-type-catalog.mjs";

const BUNDLED = [
  { typeId: "PEDIMENTO_ERP", displayName: "Pedimento (mapeo ERP)", description: "d1", fields: { A: {} } },
  { typeId: "INE", displayName: "INE", description: "d2", fields: { B: {} } },
  { typeId: "_shared_helper", displayName: "no debe salir", description: null, fields: null },
];

test("DynamoDB override replaces the bundled entry for the same type", () => {
  const merged = mergeDocumentTypes(BUNDLED, [
    { type_id: "INE", display_name: "INE (hot-reload)", description: "d2-nuevo", fields: { C: {} } },
  ]);
  const ine = merged.find((t) => t.typeId === "INE");
  assert.equal(ine.displayName, "INE (hot-reload)");
  assert.equal(ine.description, "d2-nuevo");
  assert.deepEqual(ine.fields, { C: {} });
});

test("enabled: false ignores the DynamoDB row and keeps the bundled version", () => {
  const merged = mergeDocumentTypes(BUNDLED, [
    { type_id: "INE", display_name: "NO DEBE GANAR", enabled: false },
  ]);
  assert.equal(merged.find((t) => t.typeId === "INE").displayName, "INE");
});

test("a DynamoDB-only type (never bundled) is still added — hot-reload without redeploy", () => {
  const merged = mergeDocumentTypes(BUNDLED, [
    { type_id: "NUEVO_TIPO", display_name: "Nuevo", description: "d3", fields: {} },
  ]);
  assert.ok(merged.some((t) => t.typeId === "NUEVO_TIPO"));
});

test("underscore-prefixed ids are never exposed as document types", () => {
  const merged = mergeDocumentTypes(BUNDLED, [{ type_id: "_internal", display_name: "x" }]);
  assert.ok(!merged.some((t) => t.typeId.startsWith("_")));
});

test("AllowedDocTypes narrows the list; an empty/absent scope leaves it untouched", () => {
  const merged = mergeDocumentTypes(BUNDLED, []);
  assert.deepEqual(
    scopeToAllowedDocTypes(merged, ["ine"]).map((t) => t.typeId),
    ["INE"]
  );
  assert.equal(scopeToAllowedDocTypes(merged, []).length, merged.length);
  assert.equal(scopeToAllowedDocTypes(merged, undefined).length, merged.length);
});

test("schema is omitted by default and only appears with an explicit opt-in", () => {
  const merged = mergeDocumentTypes(BUNDLED, []);
  const page = paginateDocumentTypes(merged, { page: 1, includeSchema: false });
  assert.ok(page.types.every((t) => !("schema" in t)));

  const withSchema = paginateDocumentTypes(merged, { page: 1, includeSchema: true });
  const ine = withSchema.types.find((t) => t.type === "INE");
  assert.deepEqual(ine.schema, { B: {} });

  const found = findDocumentType(merged, "ine", true);
  assert.deepEqual(found.schema, { B: {} });
  assert.ok(!("schema" in findDocumentType(merged, "ine", false)));
});

test("pagination is fixed at 5 per page and clamps out-of-range pages", () => {
  const many = Array.from({ length: 12 }, (_, i) => ({
    typeId: `T${String(i).padStart(2, "0")}`,
    displayName: `T${i}`,
    description: null,
    fields: null,
  }));
  assert.equal(DOCUMENT_TYPES_PAGE_SIZE, 5);

  const p1 = paginateDocumentTypes(many, { page: 1 });
  assert.equal(p1.types.length, 5);
  assert.equal(p1.total, 12);
  assert.equal(p1.totalPages, 3);

  const p3 = paginateDocumentTypes(many, { page: 3 });
  assert.equal(p3.types.length, 2);

  // Out of range clamps to the last page instead of erroring or returning empty.
  const tooFar = paginateDocumentTypes(many, { page: 99 });
  assert.equal(tooFar.page, 3);
  assert.equal(tooFar.types.length, 2);

  const tooLow = paginateDocumentTypes(many, { page: 0 });
  assert.equal(tooLow.page, 1);
});

test("findDocumentType matches the TYPE_ID case-insensitively and exactly, never by alias", () => {
  const merged = mergeDocumentTypes(BUNDLED, []);
  assert.equal(findDocumentType(merged, "pedimento_erp", false).type, "PEDIMENTO_ERP");
  assert.equal(findDocumentType(merged, "PEDIMENTO_ERP", false).type, "PEDIMENTO_ERP");
  assert.equal(findDocumentType(merged, "pedimento", false), null);
  assert.equal(findDocumentType(merged, "", false), null);
});

test("parseIncludeSchema and parsePage accept only well-formed input", () => {
  assert.equal(parseIncludeSchema("1"), true);
  assert.equal(parseIncludeSchema("true"), true);
  assert.equal(parseIncludeSchema("yes"), false);
  assert.equal(parseIncludeSchema(undefined), false);

  assert.equal(parsePage("3"), 3);
  assert.equal(parsePage("0"), 1);
  assert.equal(parsePage("-5"), 1);
  assert.equal(parsePage("abc"), 1);
  assert.equal(parsePage(undefined), 1);
});
