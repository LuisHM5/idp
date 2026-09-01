/**
 * Filtros de GET /documents: parseo del query string y reglas de match.
 *
 * Módulo puro sin imports de AWS a propósito — el SDK lo provee el runtime de
 * Lambda y no está instalado aquí, así que la lógica de filtrado vive aparte
 * (search-filters.mjs) para poder probarse de verdad en vez de solo leerse.
 *
 *   node --test tests/unit/node/
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  buildFilters,
  buildMetaFilters,
  buildSystems,
  matchField,
  matchesMeta,
  matchesSystems,
} from "../../../src/document-processing/resources/query-lambda/search-filters.mjs";

/** Un evento de API Gateway con el query string repetido, como llega de verdad. */
const qs = (pairs) => {
  const params = {};
  const multi = {};
  for (const [k, v] of pairs) {
    params[k] = v; // API Gateway deja SOLO el último valor aquí
    (multi[k] ||= []).push(v);
  }
  return { params, multi };
};

// --- field/value: varios filtros ------------------------------------------

test("un solo par field/value se lee igual que antes de que hubiera varios", () => {
  const { params, multi } = qs([["field", "NUMERO_PEDIMENTO"], ["value", "26 07 0823 6001630"]]);
  const { filters } = buildFilters(params, multi);
  assert.deepEqual(filters, [
    { field: "NUMERO_PEDIMENTO", value: "26 07 0823 6001630", op: "eq" },
  ]);
});

test("field/value repetidos se emparejan por posición", () => {
  const { params, multi } = qs([
    ["field", "RFC_IMPORTADOR_EXPORTADOR"], ["value", "OFU910626UQ0"],
    ["field", "PATENTE"], ["value", "3456"],
  ]);
  const { filters } = buildFilters(params, multi);
  assert.deepEqual(filters.map((f) => [f.field, f.value]), [
    ["RFC_IMPORTADOR_EXPORTADOR", "OFU910626UQ0"],
    ["PATENTE", "3456"],
  ]);
});

test("sin el mapa multi-valor (invocación directa) sigue leyendo un filtro", () => {
  const { filters } = buildFilters({ field: "PATENTE", value: "3456" }, null);
  assert.equal(filters.length, 1);
  assert.equal(filters[0].field, "PATENTE");
});

test("un solo op aplica a todos los filtros", () => {
  const { params, multi } = qs([
    ["field", "A"], ["value", "1"],
    ["field", "B"], ["value", "2"],
    ["op", "contains"],
  ]);
  const { filters } = buildFilters(params, multi);
  assert.deepEqual(filters.map((f) => f.op), ["contains", "contains"]);
});

test("un op por filtro respeta el orden", () => {
  const { params, multi } = qs([
    ["field", "A"], ["value", "1"], ["op", "eq"],
    ["field", "B"], ["value", "2"], ["op", "contains"],
  ]);
  const { filters } = buildFilters(params, multi);
  assert.deepEqual(filters.map((f) => f.op), ["eq", "contains"]);
});

test("op desconocido cae a eq, no revienta", () => {
  const { filters } = buildFilters({ field: "A", value: "1", op: "regex" }, null);
  assert.equal(filters[0].op, "eq");
});

test("field sin value es 400, con el mensaje de siempre", () => {
  const { error } = buildFilters({ field: "PATENTE" }, null);
  assert.equal(error.code, "MISSING_PARAMS");
  assert.equal(error.message, "'field' requires 'value'");
});

test("desbalance entre field y value es 400 y no una pareja inventada", () => {
  const { params, multi } = qs([
    ["field", "A"], ["value", "1"],
    ["field", "B"],
  ]);
  const { error, filters } = buildFilters(params, multi);
  assert.equal(filters, undefined);
  assert.equal(error.code, "MISSING_PARAMS");
  assert.match(error.message, /2 'field' and 1 'value'/);
});

test("un op de más para dos filtros es 400 en vez de adivinar a cuál iba", () => {
  const { params, multi } = qs([
    ["field", "A"], ["value", "1"],
    ["field", "B"], ["value", "2"],
    ["field", "C"], ["value", "3"],
    ["op", "eq"], ["op", "contains"],
  ]);
  const { error } = buildFilters(params, multi);
  assert.equal(error.code, "INVALID_PARAMS");
});

test("sin field ni value no hay filtros ni error", () => {
  assert.deepEqual(buildFilters({ docType: "PEDIMENTO_ADUANAL" }, null), { filters: [] });
});

test("`path` sigue siendo alias de `field`", () => {
  const { filters } = buildFilters({ path: "extractedEntities.PATENTE", value: "3456" }, null);
  assert.equal(filters[0].field, "extractedEntities.PATENTE");
});

// --- meta.<clave> ---------------------------------------------------------

test("meta.<clave> se lee en minúsculas, como S3 la guardó", () => {
  assert.deepEqual(buildMetaFilters({ "meta.Lote": "A123" }, null), [
    { key: "lote", values: ["A123"] },
  ]);
});

test("claves distintas se acumulan, ordenadas para una respuesta estable", () => {
  const filters = buildMetaFilters({ "meta.sucursal": "CHIH", "meta.lote": "A123" }, null);
  assert.deepEqual(filters.map((f) => f.key), ["lote", "sucursal"]);
});

test("la misma clave repetida acumula valores (OR dentro de la clave)", () => {
  const { params, multi } = qs([["meta.lote", "A123"], ["meta.lote", "B456"]]);
  assert.deepEqual(buildMetaFilters(params, multi), [
    { key: "lote", values: ["A123", "B456"] },
  ]);
});

test("una clave con comas también es OR", () => {
  assert.deepEqual(buildMetaFilters({ "meta.lote": "A123, B456" }, null), [
    { key: "lote", values: ["A123", "B456"] },
  ]);
});

test("params que no son meta.* se ignoran, y `meta.` a secas no es una clave", () => {
  assert.deepEqual(buildMetaFilters({ docType: "X", metaOp: "contains", "meta.": "y" }, null), []);
});

test("claves distintas son AND: fallar una descarta el documento", () => {
  const filters = buildMetaFilters({ "meta.lote": "A123", "meta.sucursal": "CHIH" }, null);
  const envelope = { metadata: { lote: "A123", sucursal: "JRZ" } };
  assert.equal(matchesMeta(envelope, filters, "eq"), false);
  assert.equal(matchesMeta({ metadata: { lote: "A123", sucursal: "CHIH" } }, filters, "eq"), true);
});

test("dentro de una clave basta un valor (OR)", () => {
  const filters = buildMetaFilters({ "meta.lote": "A123,B456" }, null);
  assert.equal(matchesMeta({ metadata: { lote: "B456" } }, filters, "eq"), true);
});

test("el match ignora mayúsculas y espacios de sobra, en clave y en valor", () => {
  const filters = buildMetaFilters({ "meta.LOTE": " a123 " }, null);
  assert.equal(matchesMeta({ metadata: { Lote: "A123" } }, filters, "eq"), true);
});

test("metaOp=contains busca subcadena", () => {
  const filters = buildMetaFilters({ "meta.folio": "4711" }, null);
  assert.equal(matchesMeta({ metadata: { folio: "ERP-4711-B" } }, filters, "eq"), false);
  assert.equal(matchesMeta({ metadata: { folio: "ERP-4711-B" } }, filters, "contains"), true);
});

test("un documento sin metadata no matchea un filtro de metadata", () => {
  const filters = buildMetaFilters({ "meta.lote": "A123" }, null);
  assert.equal(matchesMeta({ tracking: {} }, filters, "eq"), false);
});

test("sin filtros de metadata todo pasa", () => {
  assert.equal(matchesMeta({ tracking: {} }, [], "eq"), true);
});

// --- system ---------------------------------------------------------------

test("system acepta lista por comas y sus alias", () => {
  assert.deepEqual(buildSystems({ system: "ERP, CEDIS" }, null), ["ERP", "CEDIS"]);
  assert.deepEqual(buildSystems({ appOrigin: "ERP" }, null), ["ERP"]);
  assert.deepEqual(buildSystems({ sistema: "ERP" }, null), ["ERP"]);
});

test("varios system son OR y no distinguen mayúsculas", () => {
  const systems = buildSystems({ system: "erp,cedis" }, null);
  assert.equal(matchesSystems({ tracking: { appOrigin: "CEDIS" } }, systems), true);
  assert.equal(matchesSystems({ tracking: { appOrigin: "PORTAL" } }, systems), false);
});

test("un documento sin appOrigin no matchea un filtro de system", () => {
  assert.equal(matchesSystems({ tracking: {} }, ["ERP"]), false);
});

test("sin filtro de system todo pasa, incluido lo que no tiene appOrigin", () => {
  assert.equal(matchesSystems({ tracking: {} }, []), true);
});

// --- matchField: la semántica que ya existía, ahora por filtro -------------

test("el dot-path exacto gana y reporta dónde coincidió", () => {
  const envelope = { extractedEntities: { PATENTE: "3456" } };
  assert.deepEqual(matchField(envelope, "extractedEntities.PATENTE", "3456", "eq"), {
    matched: true,
    value: "3456",
    where: "extractedEntities.PATENTE",
  });
});

test("basta el nombre del campo: cae a búsqueda profunda y lo marca con *.", () => {
  const envelope = { expediente: { documents: [{ campos: { PATENTE: "3456" } }] } };
  const m = matchField(envelope, "PATENTE", "3456", "eq");
  assert.equal(m.matched, true);
  assert.equal(m.where, "*.PATENTE");
});
