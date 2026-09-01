/**
 * Parseo del body de POST /upload: JSON y x-www-form-urlencoded.
 *
 * Módulo puro sin imports de AWS a propósito — el SDK lo provee el runtime de
 * Lambda y no está instalado aquí, así que la lógica de parseo vive aparte
 * para poder probarse de verdad en vez de solo leerse.
 *
 *   node --test tests/unit/node/
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizeMetadata,
  parseBody,
} from "../../../src/document-processing/resources/upload-lambda/body-parser.mjs";

const form = (body, extra = {}) => ({
  headers: { "content-type": "application/x-www-form-urlencoded" },
  body,
  ...extra,
});

// --- x-www-form-urlencoded ------------------------------------------------

test("lee los campos de un form", () => {
  const { body } = parseBody(form("fileName=pedimento.pdf&documentType=PEDIMENTO_ADUANAL"));
  assert.equal(body.fileName, "pedimento.pdf");
  assert.equal(body.documentType, "PEDIMENTO_ADUANAL");
});

test("el charset del Content-Type no estorba", () => {
  const { body } = parseBody({
    headers: { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8" },
    body: "fileName=a.pdf",
  });
  assert.equal(body.fileName, "a.pdf");
});

test("acepta el header con cualquier capitalización", () => {
  const { body } = parseBody({
    headers: { "Content-Type": "APPLICATION/X-WWW-FORM-URLENCODED" },
    body: "fileName=a.pdf",
  });
  assert.equal(body.fileName, "a.pdf");
});

test("decodifica escapes de formulario", () => {
  const { body } = parseBody(form(`fileName=${encodeURIComponent("mi pedimento (1).pdf")}`));
  assert.equal(body.fileName, "mi pedimento (1).pdf");
});

test("un '+' en un form es un espacio, no un literal", () => {
  assert.equal(parseBody(form("fileName=a+b.pdf")).body.fileName, "a b.pdf");
});

test("metadata viaja como JSON en string y se parsea a objeto", () => {
  const { body } = parseBody(form("fileName=a.pdf&metadata=" + encodeURIComponent('{"origen":"SAP"}')));
  assert.deepEqual(body.metadata, { origen: "SAP" });
});

test("metadata mal formada se rechaza con un código propio", () => {
  const { error, body } = parseBody(form("fileName=a.pdf&metadata=no-soy-json"));
  assert.equal(body, undefined);
  assert.equal(error.code, "INVALID_METADATA");
  assert.match(error.message, /metadata/);
});

test("metadata vacía se deja pasar sin intentar parsearla", () => {
  const { body, error } = parseBody(form("fileName=a.pdf&metadata="));
  assert.equal(error, undefined);
  assert.equal(body.metadata, "");
});

// --- JSON -----------------------------------------------------------------

test("JSON es el default cuando no hay Content-Type", () => {
  const { body } = parseBody({ body: '{"fileName":"a.pdf"}' });
  assert.equal(body.fileName, "a.pdf");
});

test("un Content-Type JSON explícito se parsea igual", () => {
  const { body } = parseBody({
    headers: { "content-type": "application/json" },
    body: '{"fileName":"a.pdf"}',
  });
  assert.equal(body.fileName, "a.pdf");
});

test("JSON inválido devuelve INVALID_JSON, no revienta", () => {
  const { error } = parseBody({ body: "{roto" });
  assert.equal(error.code, "INVALID_JSON");
});

test("un body que ya viene como objeto se usa tal cual", () => {
  const { body } = parseBody({ body: { fileName: "a.pdf" } });
  assert.equal(body.fileName, "a.pdf");
});

// --- casos borde ----------------------------------------------------------

test("body vacío, nulo o ausente da un objeto vacío, no un error", () => {
  for (const body of ["", null, undefined]) {
    const out = parseBody({ body });
    assert.equal(out.error, undefined);
    assert.deepEqual(out.body, {});
  }
});

test("un body en base64 (API Gateway) se decodifica antes de parsear", () => {
  const encoded = Buffer.from("fileName=a.pdf").toString("base64");
  const { body } = parseBody(form(encoded, { isBase64Encoded: true }));
  assert.equal(body.fileName, "a.pdf");
});

test("base64 también aplica al camino JSON", () => {
  const encoded = Buffer.from('{"fileName":"a.pdf"}').toString("base64");
  const { body } = parseBody({ body: encoded, isBase64Encoded: true });
  assert.equal(body.fileName, "a.pdf");
});

// --- flujo único ----------------------------------------------------------

test("contentBase64 sigue parseándose: el handler lo rechaza, no el parser", () => {
  // El 400 INLINE_UPLOAD_REMOVED se decide en index.mjs. Aquí solo importa que
  // el campo llegue intacto para que ese rechazo pueda dispararse — si el
  // parser lo tirara, un cliente viejo recibiría una URL prefirmada y creería
  // que subió el archivo.
  const b64 = Buffer.from("%PDF-1.7").toString("base64");
  const { body } = parseBody(form(`fileName=a.pdf&contentBase64=${encodeURIComponent(b64)}`));
  assert.equal(Buffer.from(body.contentBase64, "base64").toString(), "%PDF-1.7");
});

// --- normalizeMetadata ----------------------------------------------------
// La metadata del POST se guarda como metadata de objeto S3 y de ahí la lee el
// consumidor SQS para dejarla en el envelope, donde `?meta.<clave>=` la filtra.
// Todo lo que S3 no aguanta se rechaza AQUÍ: en el PUT prefirmado el mismo
// error aparece como un 403 de S3 que parece de permisos y no lo es.

test("baja las claves a minúsculas, como haría S3 de todos modos", () => {
  const { metadata } = normalizeMetadata({ Lote: "A123", SUCURSAL: "CHIH" });
  assert.deepEqual(metadata, { lote: "A123", sucursal: "CHIH" });
});

test("los escalares se vuelven string", () => {
  const { metadata } = normalizeMetadata({ piezas: 12, urgente: true });
  assert.deepEqual(metadata, { piezas: "12", urgente: "true" });
});

test("descarta vacíos y nulos en vez de guardar claves sin valor", () => {
  const { metadata } = normalizeMetadata({ a: "", b: null, c: undefined, d: "  ", e: "ok" });
  assert.deepEqual(metadata, { e: "ok" });
});

test("una clave reservada del sistema se ignora: nadie suplanta a otra app", () => {
  const { metadata } = normalizeMetadata({ "app-origin": "OTRA_APP", "model-id": "x", lote: "A1" });
  assert.deepEqual(metadata, { lote: "A1" });
});

test("la reservada también se ignora escrita con mayúsculas", () => {
  const { metadata } = normalizeMetadata({ "App-Origin": "OTRA_APP" });
  assert.deepEqual(metadata, {});
});

test("sin metadata devuelve un objeto vacío, no un error", () => {
  assert.deepEqual(normalizeMetadata(undefined), { metadata: {} });
  assert.deepEqual(normalizeMetadata(null), { metadata: {} });
});

test("un valor anidado es 400: la metadata de S3 es plana", () => {
  const { error } = normalizeMetadata({ lote: { id: "A123" } });
  assert.equal(error.code, "INVALID_METADATA");
  assert.match(error.message, /metadata\.lote/);
});

test("un arreglo en la raíz también es 400", () => {
  assert.equal(normalizeMetadata([{ lote: "A1" }]).error.code, "INVALID_METADATA");
});

test("un acento es 400 aquí, no un 403 de S3 media hora después", () => {
  const { error } = normalizeMetadata({ sucursal: "Chihuahua Centro ñ" });
  assert.equal(error.code, "INVALID_METADATA");
  assert.match(error.message, /US-ASCII/);
});

test("pasarse de 2KB es 400, con el tamaño real en el mensaje", () => {
  const { error } = normalizeMetadata({ nota: "x".repeat(2100) });
  assert.equal(error.code, "INVALID_METADATA");
  assert.match(error.message, /2104 bytes/);
});

test("justo debajo del tope pasa", () => {
  const { metadata } = normalizeMetadata({ nota: "x".repeat(2044) });
  assert.equal(metadata.nota.length, 2044);
});
