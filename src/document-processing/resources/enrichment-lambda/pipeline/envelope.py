"""Envelope v2: un documento = un elemento de `documents[]`.

El envelope es lo que la Lambda devuelve a Step Functions, lo que se guarda en
DynamoDB (`EnrichmentResult`), lo que se publica en EventBridge y lo que
`GET /documents/{id}` regresa como `result`. Antes de v2 tenía el mismo dato
hasta tres veces y, con varios documentos en un archivo, mentía:

- `extractedEntities` de la raíz era el árbol completo del PRIMER documento
  principal, presentado como si fuera "el documento". Un PDF con dos facturas
  contestaba con el número de la primera y nada decía que hubiera una segunda.
- `expediente.documents[].extractedEntities` repetía esos mismos valores como
  escalares pelones, con otra forma.
- Los renglones (CONCEPTOS, PARTIDAS) solo viajaban para el primer documento;
  para el resto quedaban en `arrayCounts`.

v2 tiene un solo lugar para cada dato: `documents[i].fields`. Ese arreglo
existe SIEMPRE, con uno o con N documentos — quien consume no puede saber de
antemano cuántos trae un PDF, así que una forma que dependa del resultado
obliga a escribir dos caminos y a equivocarse el día que llegan dos.

La raíz espeja la raíz del resultado completo de S3 (`results/{id}.json`), que
ya tiene `documents`/`crossDocumentChecks`/`pages` arriba: `result` y
`fullResult` dejan de ser dos estructuras distintas y pasan a ser la misma, una
más profunda que la otra.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

ENVELOPE_VERSION = 2

# Tipo con el que se etiqueta un archivo que trae documentos de tipos DISTINTOS.
# Cuando todos son del mismo tipo, el envelope reporta ese tipo (ver
# `classification_of`).
CONTAINER_TYPE = "EXPEDIENTE_ADUANAL_MULTIDOCUMENTO"

# Vocabulario del pipeline -> vocabulario del API.
STATUS_MAP = {
    "APPROVED": "APROBADO",
    "REJECTED": "RECHAZADO",
    "PARTIALLY_APPROVED": "REQUIERE_REVISION",
    "REVIEW_REQUIRED": "REQUIERE_REVISION",
    "PROCESSING_ERROR": "REQUIERE_REVISION",
}

# El flujo estándar solo conoce el vocabulario del API; sus documentos se
# reportan con el del pipeline para que `documents[i].status` signifique lo
# mismo venga de donde venga.
STATUS_MAP_REVERSE = {
    "APROBADO": "APPROVED",
    "RECHAZADO": "REJECTED",
    "REQUIERE_REVISION": "REVIEW_REQUIRED",
}

# Presupuesto de bytes del envelope serializado. El límite que manda NO es
# DynamoDB (400 KB por item) sino Step Functions (256 KB de payload de estado):
# el envelope es el valor de retorno de la Lambda y viaja por el state machine
# hasta el DynamoUpdateItem que escribe `EnrichmentResult`. 180 KB deja aire
# para el resto del estado y para las otras columnas del item.
BUDGET_BYTES = int(os.environ.get("ENVELOPE_BUDGET_BYTES", "180000"))


def _size(obj: Any) -> int:
    """Bytes UTF-8 del objeto serializado. UTF-8 y no `len()` de la cadena:
    los acentos ocupan dos bytes y son justo lo que abunda en estos documentos."""
    return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))


# --------------------------------------------------------------- campos

def compact_node(node: Any) -> Optional[Dict[str, Any]]:
    """Un campo resuelto en su forma mínima, o None si no hay nada que decir.

    Escalar -> {value, confidence, valid} + `reason` solo si `valid` es falso
    + `source` solo si NO es MODEL_EXTRACTION (o sea, solo cuando algo
    interesante pasó: normalizado, recuperado del QR, derivado).
    Objeto   -> {fields: {...}}   Arreglo -> {items: [{fields: {...}}]}

    **Regla de omisión: un campo se omite si NO tiene valor Y `valid` es true.**
    Eso significa exactamente "el documento no lo traía y nadie lo exigía". Un
    obligatorio ausente conserva su `valid: false` y su `reason`; un valor
    descartado por confianza baja también (`value: null` más el `reason` que lo
    explica). No se pierde nada informativo — se va el ruido de
    `{"value": null, "confidence": 0.0, "valid": true, "reason": null,
    "source": "MODEL_EXTRACTION"}`, que en un tipo con 66 campos era el 20% de
    la respuesta.
    """
    if not isinstance(node, dict):
        return None

    if isinstance(node.get("items"), list):
        # Las filas NUNCA se descartan: `len(items)` es la señal de cuántos
        # renglones traía el documento, y perderla es peor que un objeto vacío.
        return {"items": [_compact_row(row) for row in node["items"]]}

    if isinstance(node.get("fields"), dict):
        sub = compact_fields(node["fields"])
        valid = node.get("valid", True)
        if not sub and valid:
            return None
        out: Dict[str, Any] = {"fields": sub}
        if not valid:
            out["valid"] = False
            if node.get("reason"):
                out["reason"] = node["reason"]
        return out

    if "value" not in node:
        return None

    value = node.get("value")
    valid = node.get("valid", True)
    if value in (None, "", []) and valid:
        return None
    # Every emitted scalar is self-contained. Pipelines that measured a value
    # provide their final number; an older/standard path with no measurement
    # says so explicitly with UNKNOWN (0.0), never through a missing key that
    # forces consumers to invent inheritance rules.
    out = {
        "value": value,
        "confidence": node.get("confidence") if node.get("confidence") is not None else 0.0,
    }
    out["valid"] = valid
    if not valid and node.get("reason"):
        out["reason"] = node["reason"]
    source = node.get("source")
    if source and source != "MODEL_EXTRACTION":
        out["source"] = source
    return out


def _compact_row(row: Any) -> Dict[str, Any]:
    if not isinstance(row, dict):
        return {"fields": {}}
    out: Dict[str, Any] = {"fields": compact_fields(row.get("fields") or {})}
    if not row.get("valid", True):
        out["valid"] = False
        if row.get("reason"):
            out["reason"] = row["reason"]
    return out


def compact_fields(tree: Dict[str, Any]) -> Dict[str, Any]:
    """`compact_node` sobre cada campo, tirando los que no dicen nada."""
    out: Dict[str, Any] = {}
    for name, node in (tree or {}).items():
        compact = compact_node(node)
        if compact is not None:
            out[name] = compact
    return out


def _legacy_fields(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Campos de un documento del DSL legado, en la MISMA forma que los del
    field-centric. `resolve_fields` ya emite {value, confidence, valid, reason}
    (más detalle que aquí no cabe y vive en S3), así que `compact_node` sirve
    tal cual; los arreglos, que ahí son listas de valores crudos, se envuelven
    para que `fields.X.items[].fields` signifique lo mismo bajo los dos DSL."""
    fields = compact_fields(doc.get("fields") or {})
    for name, value in (doc.get("extractedEntities") or {}).items():
        if not isinstance(value, list) or name in fields:
            continue
        fields[name] = {
            "items": [
                {"fields": {k: {"value": v, "confidence": 0.0} for k, v in row.items()}}
                if isinstance(row, dict)
                else {"fields": {}}
                for row in value
            ]
        }
    return fields


# ------------------------------------------------------------ documentos

def _is_missing_requested_marker(doc: Dict[str, Any]) -> bool:
    return (
        "pageStart" in doc
        and doc.get("pageStart") is None
        and "pageEnd" in doc
        and doc.get("pageEnd") is None
    )


def _is_main_document(doc: Dict[str, Any]) -> bool:
    """Whether a row represents a document that was actually found.

    Requested-type markers deliberately have no page range. They stay visible
    in `documents[]`, but do not count as found content or influence the file's
    classification/count summaries.
    """
    return (
        doc.get("documentType") != "OTRO"
        and not _is_missing_requested_marker(doc)
    )


def document_view(doc: Dict[str, Any], index: int) -> Dict[str, Any]:
    """Un elemento de `documents[]`.

    Las llaves del envelope van primero y `fields` al final a propósito: es lo
    más grande, y así los datos de identificación se leen sin desplazarse.
    """
    if doc.get("fieldTreeShape"):
        fields = compact_fields(doc.get("extractedEntities") or {})
    else:
        fields = _legacy_fields(doc)

    view: Dict[str, Any] = {"index": index, "documentType": doc.get("documentType")}
    if doc.get("documentSubtype"):
        view["documentSubtype"] = doc["documentSubtype"]
    if doc.get("businessDestination"):
        view["businessDestination"] = doc["businessDestination"]
    view["pageStart"] = doc.get("pageStart")
    view["pageEnd"] = doc.get("pageEnd")
    view["status"] = doc.get("status")
    view["confidence"] = doc.get("confidence")
    if doc.get("error"):
        view["error"] = doc["error"]
        if doc.get("errorType"):
            view["errorType"] = doc["errorType"]
    if doc.get("recoveredFields"):
        view["recoveredFields"] = doc["recoveredFields"]
    if doc.get("liquidacionNormalizada"):
        view["liquidacionNormalizada"] = doc["liquidacionNormalizada"]
    # Calculated during validation, while those confidences are already being
    # traversed for the document aggregate. Copying the summary here is O(1),
    # and—critically—never removes `confidence` from an individual field.
    if doc.get("fieldConfidence") is not None:
        view["fieldConfidence"] = doc["fieldConfidence"]
    # Physical-authenticity stage (pipeline/authenticity.py): the caller gets
    # the verdict and the REASONS — they are what a reviewer acts on and what a
    # supplier is told — but not the boxes, per-page statistics, fingerprints
    # and image hashes behind them, which stay in the full S3 result.
    if doc.get("authenticity"):
        view["authenticity"] = _authenticity_view(doc["authenticity"])
    # Per-document verdict of the type check (pipeline/type_check). The root
    # carries the file-level aggregate; this is where a bundle says WHICH
    # document was not what it claimed. Omitted when the stage did not run.
    if isinstance(doc.get("typeCheck"), dict) and doc["typeCheck"].get("evaluated"):
        tc = doc["typeCheck"]
        view["typeCheck"] = {
            "matches": tc.get("matches"),
            "rejectionConfidence": tc.get("rejectionConfidence", 0.0),
            "detected": tc.get("detected"),
            **({"reason": tc["reason"]} if tc.get("reason") else {}),
        }
    extraction_meta = doc.get("extractionMeta") or {}
    deterministic = extraction_meta.get("deterministicExtraction")
    if isinstance(deterministic, dict):
        token_savings = deterministic.get("tokenSavings") or {}
        view["deterministicExtraction"] = {
            "strategy": extraction_meta.get("strategy"),
            "aiExecuted": bool(
                int(token_savings.get("billedInputTokens") or 0)
                or int(token_savings.get("billedOutputTokens") or 0)
            ),
            "resolvedFieldCount": len(deterministic.get("resolvedFields") or []),
            "fallbackFields": deterministic.get("fallbackFields") or [],
            "tokenSavings": token_savings,
        }
    view["fields"] = fields
    return view


def type_match_view(aggregate: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Las tres llaves de la raíz del envelope que dicen si el archivo ES lo
    que se declaró (pipeline/type_check.aggregate). SIEMPRE presentes, para
    que un consumidor no tenga que preguntar si existen: `null`/`0.0`/`null`
    significa "no se evaluó" (XML, tabular, flujo estándar, kill switch)."""
    agg = aggregate if isinstance(aggregate, dict) else {}
    matches = agg.get("matches")
    return {
        "documentMatchesType": matches if isinstance(matches, bool) else None,
        "rejectionConfidence": float(agg.get("rejectionConfidence") or 0.0),
        "detectedDocument": agg.get("detected") or None,
    }


def ai_execution_view(
    usage: Optional[Dict[str, Any]], tracking: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """One unambiguous answer to whether an AI model really ran.

    ``tracking.modelIds`` records actual model invocations, including the two
    upstream Lambda phases whose token usage is not always propagated here.
    Token totals include only stages whose raw Bedrock usage reached this
    envelope; ``tokenReportedStages`` makes that boundary explicit.
    Textract OCR is deliberately not counted as generative AI.
    """
    usage = usage or {}
    tracking = tracking or {}
    stages = set((tracking.get("modelIds") or {}).keys())
    reported = set()
    input_tokens = output_tokens = images = 0
    for stage, payload in usage.items():
        if not isinstance(payload, dict) or stage == "pageSelection":
            continue
        raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        stage_input = int(raw.get("inputTokens") or 0)
        stage_output = int(raw.get("outputTokens") or 0)
        stage_images = int(payload.get("images") or 0)
        if stage_input or stage_output or stage_images:
            stages.add(stage)
            reported.add(stage)
            input_tokens += stage_input
            output_tokens += stage_output
            images += stage_images
    order = {
        name: index for index, name in enumerate(
            ("classification", "processing", "segmentation", "pageLocator", "extraction", "authenticity", "embeddings")
        )
    }
    ordered = sorted(stages, key=lambda name: (order.get(name, 99), name))
    reported_ordered = [name for name in ordered if name in reported]
    return {
        "executed": bool(ordered),
        "stages": ordered,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "images": images,
        "tokenReportedStages": reported_ordered,
    }


def deterministic_savings_view(documents: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Aggregate per-document partial-schema counterfactuals."""
    savings = []
    for doc in documents:
        block = ((doc.get("extractionMeta") or {}).get("deterministicExtraction") or {})
        token_savings = block.get("tokenSavings")
        if isinstance(token_savings, dict):
            savings.append(token_savings)
    if not savings:
        return None

    def total(name: str) -> int:
        return sum(int(item.get(name) or 0) for item in savings)

    billed_input = total("billedInputTokens")
    billed_output = total("billedOutputTokens")
    saved_input = total("estimatedInputTokensSaved")
    saved_output = total("estimatedOutputTokensSaved")
    without_input = billed_input + saved_input
    without_output = billed_output + saved_output
    return {
        "estimated": True,
        "method": "partial_schema_counterfactual_v1",
        "documentsMeasured": len(savings),
        "billedInputTokens": billed_input,
        "billedOutputTokens": billed_output,
        "estimatedInputTokensSaved": saved_input,
        "estimatedOutputTokensSaved": saved_output,
        "estimatedInputTokensWithoutDeterministic": without_input,
        "estimatedOutputTokensWithoutDeterministic": without_output,
        "estimatedInputSavingPct": round(saved_input / without_input * 100, 1) if without_input else 0.0,
        "estimatedOutputSavingPct": round(saved_output / without_output * 100, 1) if without_output else 0.0,
    }


def _authenticity_view(audit: Dict[str, Any]) -> Dict[str, Any]:
    # Mirrors `pipeline.authenticity.envelope_view`; duplicated rather than
    # imported because this module is deliberately free of the pipeline's
    # imaging/AWS imports so it stays unit-testable on its own.
    return {
        "verdict": audit.get("verdict"),
        "layers": audit.get("layers") or {},
        "reasons": [
            {"code": r.get("code"), "hard": bool(r.get("hard")), "message": r.get("message")}
            for r in audit.get("reasons") or []
        ],
        "referencesFound": bool((audit.get("references") or {}).get("found")),
    }


def classification_of(documents: List[Dict[str, Any]], fallback: str) -> str:
    """El tipo con el que se etiqueta TODO el archivo.

    Si todos los documentos principales son del mismo tipo, ese es el tipo —
    aunque sean varios. Un PDF con dos facturas extranjeras es un archivo de
    facturas extranjeras, no un contenedor genérico: etiquetarlo
    `EXPEDIENTE_ADUANAL_MULTIDOCUMENTO` lo escondía de
    `GET /documents?docType=FACTURA_...` (que solo lo encontraba por el rodeo de
    `ContainedDocumentTypes`) y de una app restringida a ese tipo por
    `AllowedDocTypes`.

    Y si son de tipos DISTINTOS, son esos tipos separados por coma, en orden de
    aparición: `PEDIMENTO_ERP,FACTURA_FLETE_EXTRANJERO` dice qué trae el archivo;
    `EXPEDIENTE_ADUANAL_MULTIDOCUMENTO` solo decía "varias cosas" y obligaba a
    recorrer `documents[]` para saber cuáles. Los `OTRO` nunca entran: son
    páginas que se conservan para que la cobertura del archivo sea auditable, no
    contenido que el archivo "sea" (por eso tampoco cuentan en `documentCount`).

    ⚠️ Esta es la etiqueta que ve QUIEN LLAMA, no la que se indexa. La columna
    `DocumentType` del GSI sigue siendo el contenedor — ver `index_type_of`.
    """
    main = [d for d in documents if _is_main_document(d)]
    if not main:
        if any(_is_missing_requested_marker(d) for d in documents):
            return "OTRO"
        return fallback
    types: List[str] = []
    for doc in main:
        doc_type = doc.get("documentType")
        if doc_type and doc_type not in types:
            types.append(doc_type)
    if not types:
        return fallback
    return ",".join(types)


def index_type_of(classification: str) -> str:
    """La clasificación reducida a UN tipo, para la columna indexada.

    `classification_of` puede devolver varios tipos separados por coma, y eso no
    puede viajar a la partition key del GSI `DocumentType`: una Query de DynamoDB
    busca por valor EXACTO de la partición, así que un expediente indexado bajo
    `A,B` sería invisible para `?docType=A`, y la Lambda de consulta no tiene
    forma de enumerar particiones sin un Scan — que es justamente lo que el par
    contenedor + `ContainedDocumentTypes` existe para evitar.

    Por eso lo mixto se sigue indexando bajo el contenedor: `expandContainers`
    (query-lambda) hace UNA Query extra por él, filtrada por
    `contains(ContainedDocumentTypes, :t)`, y así `?docType=PEDIMENTO_ERP`
    encuentra el pedimento que vive dentro del expediente mixto. La etiqueta
    legible y la llave de búsqueda son dos necesidades distintas.
    """
    return CONTAINER_TYPE if "," in (classification or "") else classification


def _summary(counts: Counter) -> str:
    total = sum(counts.values())
    if not total:
        return "sin documentos"
    detail = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    return f"{total} documento(s) — {detail}"


# -------------------------------------------------------- presupuesto

def _array_nodes(env: Dict[str, Any]):
    """(contenedor, llave, ruta, bytes) de cada campo tipo arreglo, a cualquier
    profundidad, para poder soltar el más grande primero."""
    def walk(fields: Dict[str, Any], path: str):
        for name, node in (fields or {}).items():
            if not isinstance(node, dict):
                continue
            here = f"{path}.{name}"
            if isinstance(node.get("items"), list):
                yield fields, name, here, _size(node)
            elif isinstance(node.get("fields"), dict):
                yield from walk(node["fields"], here)

    for i, doc in enumerate(env.get("documents") or []):
        yield from walk(doc.get("fields") or {}, f"documents[{i}].fields")


def fit_to_budget(
    env: Dict[str, Any], limit: Optional[int] = None
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Recorta el envelope hasta que quepa, de forma determinista y anunciada.

    Suelta primero el arreglo MÁS GRANDE de todos los documentos, no los del
    último: así una factura con tres conceptos nunca pierde sus renglones por
    culpa de un pedimento con cuatrocientas partidas. Si aun soltándolos todos
    no cabe, se van los campos del documento más pesado.

    Nada se pierde de verdad: el resultado completo ya está en S3 ANTES de este
    recorte (`handler.py` escribe `results/{id}.json` primero), así que
    `resultLocation` y `?full=1` siempre tienen todo. Y nunca es silencioso: lo
    que se soltó queda listado en `omitted`.
    """
    limit = limit or BUDGET_BYTES
    size = _size(env)
    if size <= limit:
        return env, None

    dropped: List[str] = []
    while size > limit:
        candidates = list(_array_nodes(env))
        if not candidates:
            break
        container, name, path, _bytes = max(candidates, key=lambda c: c[3])
        rows = container[name].get("items") or []
        container[name] = {"count": len(rows), "omitted": "size"}
        dropped.append(path)
        size = _size(env)

    report: Dict[str, Any] = {"reason": "size", "limitBytes": limit, "fields": dropped}
    if size > limit:
        # Último recurso: los campos del documento más pesado, empezando por
        # ese. Se conserva el encabezado (tipo, páginas, status) para que el
        # documento siga siendo visible y localizable en S3.
        for doc in sorted(env.get("documents") or [], key=lambda d: -_size(d)):
            if size <= limit:
                break
            if doc.get("fields"):
                doc["fields"] = {}
                doc["fieldsOmitted"] = "size"
                dropped.append(f"documents[{doc.get('index', 0) - 1}].fields")
                size = _size(env)
    report["bytes"] = size
    return env, report


# ---------------------------------------------------------------- build

def build(
    *,
    document_id: str,
    result: Dict[str, Any],
    tracking: Dict[str, Any],
    metadata: Optional[Dict[str, str]],
    result_location: Optional[Dict[str, str]],
    fallback_classification: str,
) -> Dict[str, Any]:
    """Envelope v2 para los pipelines multi-documento (expediente/XML/tabular)."""
    documents = result.get("documents") or []
    views = [document_view(d, i) for i, d in enumerate(documents, 1)]
    main = [d for d in documents if _is_main_document(d)]
    counts = Counter(d.get("status") for d in main if d.get("status"))
    ai_usage = ai_execution_view(result.get("usage"), tracking)

    env: Dict[str, Any] = {
        "documentId": document_id,
        "envelopeVersion": ENVELOPE_VERSION,
        "documentClassification": classification_of(documents, fallback_classification),
        **type_match_view(result.get("typeCheck")),
        "aiExecuted": ai_usage["executed"],
        "aiUsage": ai_usage,
        "documentCount": len(main),
        "tracking": tracking,
        **({"metadata": metadata} if metadata else {}),
        "validation": {
            "status": STATUS_MAP.get(result.get("status"), "REQUIERE_REVISION"),
            "reviewRequired": bool(result.get("reviewRequired")),
            "summary": _summary(counts),
            "counts": dict(counts),
        },
        "documents": views,
    }
    deterministic_savings = deterministic_savings_view(documents)
    if deterministic_savings is not None:
        env["deterministicSavings"] = deterministic_savings
    # Solo las verificaciones que REALMENTE corrieron. Una regla `skipped` es
    # la ausencia de información, no información: en un archivo de facturas
    # extranjeras, 4 de las 5 reglas cruzadas del catálogo aduanal salían
    # `skipped` y ocupaban 628 de los 872 bytes del bloque, sin decir nada.
    # Siguen completas en `fullResult` (`crossDocumentChecks`) para auditar
    # qué se evaluó y qué no.
    #
    # Un solo nombre para "verificaciones de nivel documento" en los dos
    # flujos: aquí son las reglas entre documentos, en el estándar son las
    # reglas del documento único.
    ran = [c for c in (result.get("crossDocumentChecks") or []) if not c.get("skipped")]
    if ran:
        env["checks"] = ran
    if result.get("warnings"):
        env["warnings"] = result["warnings"]
    if result.get("error"):
        # El archivo entero falló (ni se pudo leer): bloque estructurado para
        # triage, sin tener que parsear el mensaje libre de `warnings`.
        env["error"] = result["error"]
    if result.get("sourceFile"):
        # El NOMBRE del archivo, no la llave de S3. La llave apuntaba a
        # `raw/{TIPO}/{id}/archivo.pdf`, que es donde el objeto vive DURANTE el
        # procesamiento: al terminar la ejecución el blueprint lo mueve a
        # `processed/{documentId}/` (o a `failed/`), así que para cuando quien
        # llama lee esta respuesta ese path ya no resuelve. Los bytes se piden
        # por `GET /documents/{id}/file`, que sí sabe dónde quedaron. La llave
        # completa sigue en `fullResult` para forense.
        env["sourceFile"] = str(result["sourceFile"]).rsplit("/", 1)[-1]
    for key in ("processingId", "sourceFormat", "pages", "usage", "truncated"):
        if result.get(key) is not None:
            env[key] = result[key]
    env["resultLocation"] = result_location
    env["processedAt"] = result.get("processedAt")

    env, omitted = fit_to_budget(env)
    if omitted:
        env["omitted"] = omitted
        # El reporte se mide antes de adjuntarlo, así que sin esto `bytes`
        # subestimaría el payload real por el tamaño del propio bloque.
        env["omitted"]["bytes"] = _size(env)
    return env


def build_standard(
    *,
    document_id: str,
    classification: str,
    entity_map: Dict[str, Any],
    checks: List[Any],
    status: str,
    tracking: Dict[str, Any],
    metadata: Optional[Dict[str, str]],
    summary: str,
) -> Dict[str, Any]:
    """Misma forma para el flujo estándar (un documento, DSL legado).

    Emite `documents[]` con un elemento en vez de un `extractedEntities` plano,
    para que un consumidor tenga UN solo parser para todo el sistema. Ese flujo
    no calcula confianza por campo, así que usa 0.0 (`UNKNOWN`): un número
    explícito que significa "no medido", nunca una confianza inventada.
    """
    failures: Dict[str, Any] = {}
    for c in checks:
        if not getattr(c, "valid", True) and getattr(c, "field", None):
            failures.setdefault(c.field, getattr(c, "message", None))

    fields: Dict[str, Any] = {}
    for name, value in (entity_map or {}).items():
        node = {"value": value, "confidence": 0.0, "valid": name not in failures}
        if name in failures and failures[name]:
            node["reason"] = failures[name]
        compact = compact_node(node)
        if compact is not None:
            fields[name] = compact

    doc_status = STATUS_MAP_REVERSE.get(status, "REVIEW_REQUIRED")
    ai_usage = ai_execution_view(None, tracking)
    return {
        "documentId": document_id,
        "envelopeVersion": ENVELOPE_VERSION,
        "documentClassification": classification,
        # El flujo estándar no pregunta al modelo si el archivo es lo que se
        # declaró: las tres llaves existen con su valor de "no evaluado".
        **type_match_view(None),
        "aiExecuted": ai_usage["executed"],
        "aiUsage": ai_usage,
        "documentCount": 1,
        "tracking": tracking,
        **({"metadata": metadata} if metadata else {}),
        "validation": {
            "status": status,
            "reviewRequired": status != "APROBADO",
            "summary": summary,
            "counts": {doc_status: 1},
        },
        "documents": [
            {
                "index": 1,
                "documentType": classification,
                "pageStart": 1,
                "pageEnd": 1,
                "status": doc_status,
                "confidence": None,
                "fields": fields,
            }
        ],
        "checks": [
            {
                "field": getattr(c, "field", None),
                "rule": getattr(c, "rule", None),
                "valid": getattr(c, "valid", True),
                "message": getattr(c, "message", None),
            }
            for c in checks
        ],
        "processedAt": tracking.get("createdAt"),
    }
