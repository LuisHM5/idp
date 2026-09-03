/**
 * Lee un envelope de resultado sin que a quien lo consume le importe su versión.
 *
 * A partir de v2 (`envelopeVersion: 2`) el envelope es plano: `documents[]` en
 * la raíz, un elemento por documento encontrado, con `fields` en la forma
 * `{value, confidence, valid}`. Antes había tres representaciones del mismo
 * dato — `extractedEntities` de la raíz (el árbol del PRIMER documento),
 * `expediente.documents[].extractedEntities` (escalares pelones) y el detalle
 * completo en S3 — y esa duplicación es justo lo que v2 elimina.
 *
 * Este módulo existe porque los envelopes VIEJOS siguen guardados en DynamoDB:
 * `GET /documents/{id}`, la búsqueda y `pnpm backfill:keys` tienen que poder
 * leerlos años después. Escribir solo se escribe v2; leer se leen los dos.
 *
 * Libre de AWS a propósito (mismo patrón que `search-filters.mjs`): así se
 * prueba sin SDK, que no está instalado localmente.
 */

/** ¿El envelope ya viene en la forma plana? */
export function isV2(envelope) {
  return Number((envelope && envelope.envelopeVersion) || 0) >= 2;
}

/**
 * Los documentos del envelope, normalizados a `{documentType, pageStart,
 * pageEnd, status, confidence, fields}` — `fields` siempre en forma de nodo
 * (`{value, ...}`), venga de donde venga.
 *
 * En v1 un envelope de flujo estándar no tenía `documents` en ningún lado: su
 * único documento era el `extractedEntities` de la raíz, y aquí se devuelve
 * como un elemento para que el consumidor no tenga que distinguir.
 */
export function readDocuments(envelope) {
  if (!envelope || typeof envelope !== "object") return [];
  if (isV2(envelope)) return envelope.documents || [];

  const legacy = (envelope.expediente && envelope.expediente.documents) || [];
  if (legacy.length) {
    return legacy.map((d, i) => ({
      index: i + 1,
      documentType: d.documentType,
      documentSubtype: d.documentSubtype,
      businessDestination: d.businessDestination,
      pageStart: d.pageStart,
      pageEnd: d.pageEnd,
      status: d.status,
      confidence: d.confidence,
      liquidacionNormalizada: d.liquidacionNormalizada,
      // v1 guardaba escalares pelones por documento; se envuelven para que
      // `fields.X.value` signifique lo mismo en las dos versiones.
      fields: asNodes(d.extractedEntities, (d.fieldsValidity || {})),
      arrayCounts: d.arrayCounts,
    }));
  }

  const entities = envelope.extractedEntities;
  if (!entities || !Object.keys(entities).length) return [];
  return [
    {
      index: 1,
      documentType: envelope.documentClassification,
      status: (envelope.validation || {}).status,
      fields: asNodes(entities, {}),
    },
  ];
}

/** Envuelve valores pelones como nodos; los que ya son nodos pasan tal cual. */
function asNodes(entities, validity) {
  const out = {};
  for (const [name, node] of Object.entries(entities || {})) {
    if (node && typeof node === "object" && !Array.isArray(node)) {
      out[name] = node;
      continue;
    }
    const extra = validity[name] || {};
    out[name] = {
      value: node,
      ...(extra.confidence !== undefined && { confidence: extra.confidence }),
      ...(extra.valid !== undefined && { valid: extra.valid }),
    };
  }
  return out;
}

/** `{FIELD: valor}` plano de un documento normalizado, para búsquedas y logs. */
export function fieldValues(doc) {
  const out = {};
  for (const [name, node] of Object.entries((doc && doc.fields) || {})) {
    if (node && typeof node === "object" && "value" in node) out[name] = node.value;
  }
  return out;
}

/** Cuántos renglones trae cada campo tipo arreglo del documento. */
export function arrayCounts(doc) {
  if (!doc) return {};
  const out = {};
  for (const [name, node] of Object.entries(doc.fields || {})) {
    if (node && Array.isArray(node.items)) out[name] = node.items.length;
    // Un arreglo soltado por presupuesto conserva su conteo (ver
    // pipeline/envelope.py::fit_to_budget).
    else if (node && typeof node.count === "number") out[name] = node.count;
  }
  // v1: el conteo venía aparte, no derivable de `fields`.
  return Object.keys(out).length ? out : doc.arrayCounts || {};
}
