/**
 * `documentType` con VARIOS tipos: parseo, modo de clasificación y llave de S3.
 *
 * El contrato que fija este archivo es sobre todo el de COMPATIBILIDAD: un solo
 * tipo tiene que seguir produciendo exactamente la misma llave y el mismo
 * `classificationMode` que antes de que la lista existiera. Si algo de eso
 * cambia, se rompe todo integrador que ya está en producción.
 *
 * Igual que `upload-body-parser.test.mjs`, prueba el módulo puro: `index.mjs`
 * importa el SDK de AWS, que el runtime de Lambda provee y aquí no está
 * instalado, así que la lógica testeable vive en `body-parser.mjs`.
 *
 *   node --test tests/unit/node/
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  classificationModeFor,
  parseDocumentTypes,
  rawKeyFor,
  serializeDocumentCounts,
} from "../../../src/document-processing/resources/upload-lambda/body-parser.mjs";

const types = (raw) => parseDocumentTypes(raw).types;

// --- parseo ----------------------------------------------------------------

test("un solo tipo sigue siendo una lista de uno", () => {
  assert.deepEqual(types("PEDIMENTO_ADUANAL"), ["PEDIMENTO_ADUANAL"]);
});

test("varios tipos separados por coma", () => {
  assert.deepEqual(types("PEDIMENTO_ADUANAL,FACTURA_HONORARIOS"), [
    "PEDIMENTO_ADUANAL",
    "FACTURA_HONORARIOS",
  ]);
});

test("tolera espacios alrededor de las comas", () => {
  assert.deepEqual(types(" PEDIMENTO_ADUANAL , FACTURA_HONORARIOS "), [
    "PEDIMENTO_ADUANAL",
    "FACTURA_HONORARIOS",
  ]);
});

test("ignora comas de más sin inventar tipos vacíos", () => {
  assert.deepEqual(types("A,,B,"), ["A", "B"]);
});

test("descarta duplicados conservando el orden de aparición", () => {
  // El orden importa: es el que se imprime en el prompt del clasificador.
  assert.deepEqual(types("B,A,B"), ["B", "A"]);
});

test("ausente, null o vacío es lista vacía (clasificación automática)", () => {
  for (const raw of [undefined, null, ""]) {
    assert.deepEqual(types(raw), [], `para ${JSON.stringify(raw)}`);
  }
});

test("un tipo mal formado es 400 y el mensaje dice cuál", () => {
  const { error } = parseDocumentTypes("PEDIMENTO_ADUANAL,factura honorarios");
  assert.equal(error.code, "INVALID_DOCUMENT_TYPE");
  assert.match(error.message, /factura honorarios/);
});

test("un documentType que no es string es 400, no un crash", () => {
  // Un caller mandando `documentType: ["A","B"]` es el error más probable
  // ahora que la lista existe; tiene que salir como 400 explicando la coma.
  const { error } = parseDocumentTypes(["A", "B"]);
  assert.equal(error.code, "INVALID_DOCUMENT_TYPE");
  assert.match(error.message, /separated by commas/);
});

test("más de 8 tipos es 400 (caben en un segmento de la llave de S3)", () => {
  const nine = ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"].join(",");
  assert.equal(parseDocumentTypes(nine).error.code, "INVALID_DOCUMENT_TYPE");
  assert.equal(types("T1,T2,T3,T4,T5,T6,T7,T8").length, 8);
});

test("pasarse de 200 caracteres es 400 aunque sean pocos tipos", () => {
  const long = ["A".repeat(64), "B".repeat(64), "C".repeat(64), "D".repeat(64)].join(",");
  const { error } = parseDocumentTypes(long);
  assert.equal(error.code, "INVALID_DOCUMENT_TYPE");
  assert.match(error.message, /S3 key/);
});

// --- classificationMode ----------------------------------------------------

test("classificationMode: los dos valores de siempre no se mueven", () => {
  assert.equal(classificationModeFor([]), "auto");
  assert.equal(classificationModeFor(["PEDIMENTO_ADUANAL"]), "manual");
});

test("classificationMode: varios tipos es 'guided'", () => {
  assert.equal(classificationModeFor(["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"]), "guided");
});

// --- llave de S3 -----------------------------------------------------------

test("la llave de un solo tipo es idéntica a la de siempre", () => {
  assert.equal(
    rawKeyFor(["PEDIMENTO_ADUANAL"], "uuid-1234", "pedimento.pdf"),
    "raw/PEDIMENTO_ADUANAL/uuid-1234/pedimento.pdf"
  );
});

test("sin tipo declarado la llave no lleva segmento de tipo", () => {
  assert.equal(rawKeyFor([], "uuid-1234", "pedimento.pdf"), "raw/uuid-1234/pedimento.pdf");
});

test("varios tipos van unidos por coma en UN solo segmento", () => {
  // Un segmento y no varios: el consumidor de SQS ancla el documentId en
  // `key_parts[-2]`, así que un segmento extra le cambiaría el id al documento.
  const key = rawKeyFor(["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"], "uuid-1234", "exp.pdf");
  assert.equal(key, "raw/PEDIMENTO_ADUANAL,FACTURA_HONORARIOS/uuid-1234/exp.pdf");
  assert.equal(key.split("/").length, 4);
});

// --- cuántos buscar por tipo (`TIPO:N`) ------------------------------------

const counts = (raw, occurrenceMode) => parseDocumentTypes(raw, occurrenceMode).counts;

test("sin occurrenceMode el upload deja el default al catálogo/pipeline", () => {
  const parsed = parseDocumentTypes("PEDIMENTO_ADUANAL");
  assert.equal(parsed.occurrenceMode, null);
  assert.deepEqual(parsed.counts, {});
});

test("varios tipos también dejan sin materializar los defaults de catálogo", () => {
  const parsed = parseDocumentTypes("PEDIMENTO_ERP,FACTURA_FLETE_EXTRANJERO");
  assert.equal(parsed.occurrenceMode, null);
  assert.deepEqual(parsed.counts, {});
});

test("single explícito limita también una lista de varios tipos", () => {
  const parsed = parseDocumentTypes("A,B", "single");
  assert.equal(parsed.occurrenceMode, "single");
  assert.deepEqual(parsed.counts, { A: 1, B: 1 });
});

test("multiple pide todas las ocurrencias de cada tipo sin sufijo", () => {
  assert.deepEqual(counts("A,B", "multiple"), { A: "all", B: "all" });
});

test("los sufijos explícitos sobrescriben occurrenceMode por tipo", () => {
  assert.deepEqual(counts("A:2,B,C:1", "multiple"), { A: 2, B: "all", C: 1 });
});

test("`:N` pide N y `:all` pide todos", () => {
  assert.deepEqual(counts("A:3,B:all"), { A: 3, B: "all" });
  assert.deepEqual(counts("A:ALL"), { A: "all" });
});

test("los tipos salen limpios: la cuenta no contamina la llave de S3", () => {
  // Los tipos viajan en la llave y las cuentas en la metadata del objeto; si
  // el `:N` se colara a los tipos, tres parsers independientes lo verían.
  const { types } = parseDocumentTypes("PEDIMENTO_ADUANAL:2,FACTURA_HONORARIOS:all");
  assert.deepEqual(types, ["PEDIMENTO_ADUANAL", "FACTURA_HONORARIOS"]);
  assert.equal(
    rawKeyFor(types, "uuid-1", "e.pdf"),
    "raw/PEDIMENTO_ADUANAL,FACTURA_HONORARIOS/uuid-1/e.pdf"
  );
});

test("un tipo repetido conserva la cuenta MAYOR, nunca la última", () => {
  // Una lista mal escrita no debe descartar documentos en silencio.
  assert.deepEqual(counts("A:2,A:5"), { A: 5 });
  assert.deepEqual(counts("A:5,A:2"), { A: 5 });
  assert.deepEqual(counts("A:2,A:all"), { A: "all" });
  assert.deepEqual(counts("A,B,A:2"), { A: 2 });
});

test("una cuenta inválida es 400, no un default silencioso", () => {
  for (const raw of ["A:0", "A:-1", "A:1.5", "A:x", "A:"]) {
    const { error } = parseDocumentTypes(raw);
    assert.equal(error?.code, "INVALID_DOCUMENT_TYPE", `para ${raw}`);
  }
});

test("occurrenceMode inválido es 400 y no cae silenciosamente a single", () => {
  for (const mode of ["all", "muchos", 1, {}]) {
    const { error } = parseDocumentTypes("A", mode);
    assert.equal(error?.code, "INVALID_OCCURRENCE_MODE", `para ${JSON.stringify(mode)}`);
  }
});

test("el tipo sigue exigiendo UPPER_SNAKE_CASE aunque ahora acepte sufijo", () => {
  // Un regex combinado con el flag `i` aceptaría minúsculas, contradiciendo
  // el propio mensaje de error.
  assert.equal(parseDocumentTypes("pedimento_erp:3").error?.code, "INVALID_DOCUMENT_TYPE");
  assert.equal(parseDocumentTypes(":2").error?.code, "INVALID_DOCUMENT_TYPE");
});

test("serializeDocumentCounts produce metadata plana US-ASCII", () => {
  assert.equal(
    serializeDocumentCounts(counts("PEDIMENTO_ERP:1,FACTURA_FLETE_EXTRANJERO:all")),
    "PEDIMENTO_ERP=1,FACTURA_FLETE_EXTRANJERO=all"
  );
  // Vacío => el caller omite la clave en vez de mandar una vacía.
  assert.equal(serializeDocumentCounts({}), "");
});
