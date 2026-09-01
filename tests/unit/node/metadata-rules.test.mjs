import test from "node:test";
import assert from "node:assert/strict";

import { normalizeMetadata } from "../../../src/document-processing/resources/upload-lambda/body-parser.mjs";
import {
  MAX_METADATA_BYTES,
  metadataBytes,
  normalizeEntry,
  parseMetadataPatch,
} from "../../../src/document-processing/resources/query-lambda/metadata-rules.mjs";

test("PATCH conserva claves ausentes y crea o reemplaza las presentes", () => {
  const out = parseMetadataPatch(
    { estado_externo: "EN_REVISION", urgente: true, intentos: 2 },
    { lote: "A123", estado_externo: "RECIBIDO" }
  );
  assert.deepEqual(out.next, {
    lote: "A123",
    estado_externo: "EN_REVISION",
    urgente: "true",
    intentos: "2",
  });
  assert.deepEqual(out.set, { estado_externo: "EN_REVISION", urgente: "true", intentos: "2" });
  assert.deepEqual(out.remove, []);
});

test("null y string vacío borran sin tocar las demás claves", () => {
  const out = parseMetadataPatch(
    { estado_externo: null, marca: "   " },
    { lote: "A123", estado_externo: "LISTO", marca: "X" }
  );
  assert.deepEqual(out.next, { lote: "A123" });
  assert.deepEqual(out.remove, ["estado_externo", "marca"]);
});

test("las claves se normalizan y la última operación normalizada gana", () => {
  const out = parseMetadataPatch({ Estado: "LISTO", ESTADO: null }, { estado: "ANTES" });
  assert.deepEqual(out.next, {});
  assert.deepEqual(out.remove, ["estado"]);
});

test("un patch vacío o compuesto solo de claves reservadas se rechaza", () => {
  assert.equal(parseMetadataPatch({}).error.code, "EMPTY_PATCH");
  const reserved = parseMetadataPatch({ "app-origin": "OTRA", "MODEL-ID": "x" });
  assert.equal(reserved.error.code, "EMPTY_PATCH");
  assert.match(reserved.error.message, /app-origin/);
});

test("el body debe ser un objeto plano", () => {
  for (const value of [null, undefined, [], "estado=LISTO", 3]) {
    assert.equal(parseMetadataPatch(value).error.code, "INVALID_METADATA");
  }
  assert.equal(parseMetadataPatch({ estado: { valor: "LISTO" } }).error.code, "INVALID_METADATA");
});

test("aplica ASCII y el tope de 2 KB sobre el resultado completo", () => {
  assert.equal(parseMetadataPatch({ nota: "revisión" }).error.code, "INVALID_METADATA");
  const tooLarge = parseMetadataPatch({ nota: "x".repeat(MAX_METADATA_BYTES) });
  assert.equal(tooLarge.error.code, "INVALID_METADATA");

  const fits = parseMetadataPatch({ nota: "x".repeat(MAX_METADATA_BYTES - 4) });
  assert.equal(metadataBytes(fits.next), MAX_METADATA_BYTES);
});

test("se puede borrar una clave y usar su espacio en el mismo PATCH", () => {
  const current = { vieja: "x".repeat(2000) };
  const out = parseMetadataPatch({ vieja: null, nueva: "y".repeat(2000) }, current);
  assert.equal(out.error, undefined);
  assert.deepEqual(Object.keys(out.next), ["nueva"]);
});

test("POST y PATCH comparten normalización y rechazo de claves/valores", () => {
  const accepted = [
    ["Estado_ERP", " LISTO "],
    ["folio.externo", 4711],
    ["marca-urgente", false],
  ];
  for (const [key, value] of accepted) {
    const upload = normalizeMetadata({ [key]: value });
    const patchEntry = normalizeEntry(key, value);
    assert.equal(upload.error, undefined);
    assert.deepEqual(upload.metadata, { [patchEntry.key]: patchEntry.value });
  }

  const rejected = [
    ["clave con espacio", "x"],
    ["-empieza-mal", "x"],
    ["estado", "revisión"],
    ["estado", ["LISTO"]],
  ];
  for (const [key, value] of rejected) {
    assert.equal(normalizeMetadata({ [key]: value }).error.code, "INVALID_METADATA");
    assert.equal(normalizeEntry(key, value).error.code, "INVALID_METADATA");
  }
});
