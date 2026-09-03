/**
 * `envelope-shape.mjs` — leer un envelope sin que importe su versión.
 *
 * Escribir se escribe solo v2 (plano, `documents[]` en la raíz). Leer hay que
 * leer las dos formas: los envelopes viejos siguen guardados en DynamoDB y
 * tanto `GET /documents/{id}` como `pnpm backfill:keys` los recorren años
 * después.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  isV2,
  readDocuments,
  fieldValues,
  arrayCounts,
} from "../../../src/document-processing/resources/query-lambda/envelope-shape.mjs";
import { matchField } from "../../../src/document-processing/resources/query-lambda/search-filters.mjs";

const V2 = {
  envelopeVersion: 2,
  documentClassification: "FACTURA_FLETE_EXTRANJERO",
  documentCount: 2,
  documents: [
    {
      index: 1,
      documentType: "FACTURA_FLETE_EXTRANJERO",
      pageStart: 1,
      pageEnd: 1,
      status: "APPROVED",
      fields: {
        NUMERO_FACTURA: { value: "SJ191703", confidence: 0.98, valid: true },
        CONCEPTOS: { items: [{ fields: { IMPORTE: { value: 14612.4, confidence: 0.9, valid: true } } }] },
      },
    },
    {
      index: 2,
      documentType: "FACTURA_FLETE_EXTRANJERO",
      pageStart: 2,
      pageEnd: 2,
      status: "REVIEW_REQUIRED",
      fields: {
        NUMERO_FACTURA: { value: "SJ191703-1", confidence: 0.98, valid: true },
        CONCEPTOS: { items: [{ fields: { IMPORTE: { value: 9741.6, confidence: 0.9, valid: true } } }] },
      },
    },
  ],
};

// Cómo se veía el MISMO PDF antes: el árbol completo del primer documento en la
// raíz, escalares pelones por documento, y los renglones del segundo perdidos.
const V1 = {
  documentClassification: "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO",
  extractedEntities: { NUMERO_FACTURA: { value: "SJ191703", confidence: 0.98 } },
  expediente: {
    documents: [
      { documentType: "FACTURA_FLETE_EXTRANJERO", pageStart: 1, pageEnd: 1,
        extractedEntities: { NUMERO_FACTURA: "SJ191703" }, arrayCounts: { CONCEPTOS: 1 } },
      { documentType: "FACTURA_FLETE_EXTRANJERO", pageStart: 2, pageEnd: 2,
        extractedEntities: { NUMERO_FACTURA: "SJ191703-1" }, arrayCounts: { CONCEPTOS: 1 } },
    ],
  },
};

const V1_ESTANDAR = {
  documentClassification: "INE",
  extractedEntities: { CURP: "ABCD000000HDFXXX00" },
  validation: { status: "APROBADO" },
};

test("distingue las dos versiones por `envelopeVersion`", () => {
  assert.equal(isV2(V2), true);
  assert.equal(isV2(V1), false);
  assert.equal(isV2(undefined), false);
});

test("las dos versiones se leen con el mismo código", () => {
  for (const envelope of [V1, V2]) {
    const docs = readDocuments(envelope);
    assert.equal(docs.length, 2);
    assert.deepEqual(docs.map((d) => fieldValues(d).NUMERO_FACTURA), ["SJ191703", "SJ191703-1"]);
    assert.deepEqual(docs.map((d) => arrayCounts(d).CONCEPTOS), [1, 1]);
  }
});

test("un envelope v1 del flujo estándar también entrega un documento", () => {
  // Ahí no había `documents` en ningún lado: el documento único era el
  // `extractedEntities` de la raíz.
  const docs = readDocuments(V1_ESTANDAR);
  assert.equal(docs.length, 1);
  assert.equal(docs[0].documentType, "INE");
  assert.equal(fieldValues(docs[0]).CURP, "ABCD000000HDFXXX00");
});

test("un envelope sin nada devuelve lista vacía, no revienta", () => {
  assert.deepEqual(readDocuments({}), []);
  assert.deepEqual(readDocuments(null), []);
});

test("v2 hace buscable un campo del SEGUNDO documento", () => {
  // Con v1 esto no matcheaba: la raíz solo traía la primera factura, y el mapa
  // compacto del segundo documento no incluía sus renglones.
  const m = matchField(V2, "NUMERO_FACTURA", "SJ191703-1", "eq");
  assert.equal(m.matched, true);
  assert.equal(m.value, "SJ191703-1");
});

test("v2 hace buscable un valor dentro de un renglón de CUALQUIER documento", () => {
  const m = matchField(V2, "IMPORTE", "9741.6", "eq");
  assert.equal(m.matched, true);

  const viejo = matchField(V1, "IMPORTE", "9741.6", "eq");
  assert.equal(viejo.matched, false, "en v1 ese dato no estaba en el envelope");
});

test("un arreglo soltado por presupuesto conserva su conteo", () => {
  const recortado = {
    envelopeVersion: 2,
    documents: [{ index: 1, documentType: "P", fields: { PARTIDAS: { count: 412, omitted: "size" } } }],
  };
  assert.deepEqual(arrayCounts(readDocuments(recortado)[0]), { PARTIDAS: 412 });
});
