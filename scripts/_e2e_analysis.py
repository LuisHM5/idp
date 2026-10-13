"""Análisis agregado de una corrida E2E sobre un dataset completo.

Puro: no llama AWS, Bedrock ni la red. Recibe los casos que produjo
`test-e2e-pedimento.py` (resumen del caso + resultado completo de S3) y
responde una sola pregunta: **¿puedo confiar en cómo este despliegue procesa
este tipo de documento?**

La confianza que reporta el modelo es auto-reportada, así que por sí sola no
prueba nada. Por eso el análisis cruza cuatro señales de naturaleza distinta:

1. **Motor de reglas** — lo que `validators/engine.py` dejó en `checks`,
   incluidas las reglas declaradas que NO corren (`skipped`, "inertes").
2. **Confianza y validez por parámetro** — por cada campo del catálogo
   (también dentro de filas de arrays), qué tan seguido se llena, qué tan
   seguido es válido y con qué confianza.
3. **Coherencia interna, sin ground truth** — invariantes del dominio que un
   valor bien leído tiene que cumplir: número de pedimento = año + aduana +
   patente + consecutivo; la referencia impresa en el nombre del archivo;
   VALOR ADUANA = valor comercial + incrementables.
4. **Ground truth** — cuando `tests/integration/expected_pedimentos.json`
   declara el archivo para este tipo.

Los nombres de campo se resuelven desde el catálogo (`pedimento_role` y alias
ERP/canónico), nunca cableados a un solo tipo.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

REPO = Path(__file__).resolve().parent.parent
CATALOG_ROOTS = (REPO / "catalogs" / "base", REPO / "catalogs" / "custom")

# Mismo default que `pipeline/expediente.py::_PEDIMENTO_ROLE_DEFAULTS`; un
# campo con `pedimento_role` en el catálogo lo sobreescribe (ver
# `pedimento_roles`). Copiado y no importado porque importar `expediente`
# arrastra boto3 y el pipeline completo a un análisis que es offline.
PEDIMENTO_ROLE_DEFAULTS = {
    "referencia": "PEDIMENTO_REFERENCE",
    "numero": "NUMERO_PEDIMENTO",
    "normalizado": "NUMERO_PEDIMENTO_NORMALIZADO",
    "anio": "ANIO_PEDIMENTO",
    "aduana": "ADUANA_DESPACHO",
    "patente": "PATENTE",
    "consecutivo": "NUMERO_CONSECUTIVO",
}
COMPONENT_WIDTH = {"anio": 2, "aduana": 2, "patente": 4, "consecutivo": 7}

# Referencia de pedimento impresa en el nombre del archivo, tal como la
# nombran agencias y el ERP: "26 07 3435 6041353 CG.pdf",
# "OPERADORA P2607 0823 6001840-1.pdf", "2607-0973-6100971_P.PDF".
FILENAME_REFERENCE_RE = re.compile(r"(?<!\d)(\d{2})[\s_-]*(\d{2})[\s_-]*(\d{4})[\s_-]*(\d{7})(?!\d)")

# Cuadre aritmético que PEDIMENTO_ADUANAL declara como `custom_formula`
# (VALOR_ADUANA = PRECIO_PAGADO_VALOR_COMERCIAL + incrementables, tolerancia
# 5). En PEDIMENTO_ERP esa regla no existe porque el evaluador de fórmulas no
# acepta nombres mixtos (ver __COMENTARIO_validaciones__ del catálogo), así
# que el test la recupera aquí. Cada operando lista sus alias por catálogo.
ARITHMETIC_CHECKS = (
    {
        "name": "Valor aduana = valor comercial + incrementables",
        "target": ("ValorAduana", "VALOR_ADUANA"),
        "operands": (
            ("Valor_ComercialPesos", "PRECIO_PAGADO_VALOR_COMERCIAL"),
            ("GI_Fletes", "FLETES"),
            ("GI_Seguros", "SEGUROS"),
            ("GI_Embalajes", "EMBALAJES"),
            ("GI_Otros", "OTROS_INCREMENTABLES"),
        ),
        "tolerance": 5.0,
    },
)

APPROVED = "APPROVED"
STATUS_ORDER = ("APPROVED", "PARTIALLY_APPROVED", "REVIEW_REQUIRED", "REJECTED", "PROCESSING_ERROR")
_INDEX_RE = re.compile(r"\[\d+\]")


# ---------------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------------

def _active_catalog_entries(roots: Iterable[Path]) -> Iterator[tuple[str, dict]]:
    """(TYPE_ID, entrada) de los catálogos activos; `save/` se ignora, igual
    que en build/sync."""
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.json")):
            if "save" in path.relative_to(root).parts:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for type_id, entry in (data.items() if isinstance(data, dict) else ()):
                if not type_id.startswith("__") and isinstance(entry, dict) and isinstance(entry.get("fields"), dict):
                    yield type_id, entry


def load_type_spec(doc_type: str, roots: Iterable[Path] = CATALOG_ROOTS) -> dict | None:
    """Entrada del tipo en los catálogos activos. None si ningún JSON la declara."""
    return next((entry for type_id, entry in _active_catalog_entries(roots) if type_id == doc_type), None)


def active_type_ids(roots: Iterable[Path] = CATALOG_ROOTS) -> list[str]:
    return sorted({type_id for type_id, _ in _active_catalog_entries(roots)})


def catalog_fields(type_spec: dict | None) -> dict[str, dict]:
    """Ruta normalizada (`A`, `A.B`, `A[].B`, `A[]`) -> tipo/required/computed."""
    out: dict[str, dict] = {}

    def walk(fields: dict, prefix: str) -> None:
        for name, spec in fields.items():
            if name.startswith("__") or not isinstance(spec, dict):
                continue
            path = f"{prefix}{name}"
            out[path] = {
                "type": spec.get("type"),
                "required": bool(spec.get("required")),
                "computed": bool(spec.get("computed") or spec.get("derived")),
            }
            if isinstance(spec.get("fields"), dict):
                walk(spec["fields"], f"{path}.")
            if isinstance(spec.get("item_fields"), dict):
                walk(spec["item_fields"], f"{path}[].")
            elif spec.get("item_type"):
                out[f"{path}[]"] = {"type": spec["item_type"], "required": False, "computed": False}

    walk((type_spec or {}).get("fields") or {}, "")
    return out


def pedimento_roles(type_spec: dict | None) -> dict[str, str]:
    """Rol -> nombre de campo, con la misma regla que
    `expediente._pedimento_field_names`."""
    names = dict(PEDIMENTO_ROLE_DEFAULTS)
    for field_name, spec in ((type_spec or {}).get("fields") or {}).items():
        declared = spec.get("pedimento_role") if isinstance(spec, dict) else None
        for role in (declared if isinstance(declared, list) else [declared] if declared else []):
            role = str(role).strip().lower()
            if role in names:
                names[role] = field_name
    return names


def unique_key_fields(type_spec: dict | None) -> list[str]:
    fields = (type_spec or {}).get("fields") or {}
    return [name for name, spec in fields.items() if isinstance(spec, dict) and spec.get("unique_key")]


# ---------------------------------------------------------------------------
# Recorrido del árbol de campos resuelto
# ---------------------------------------------------------------------------

def norm_path(path: str) -> str:
    return _INDEX_RE.sub("[]", path or "")


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def iter_leaves(entities: dict) -> Iterator[tuple[str, dict]]:
    """(ruta normalizada, hoja escalar) para cada hoja del árbol de
    `extractedEntities` (forma de `validators.engine.resolve_field_tree`)."""
    def walk(node: Any, path: str) -> Iterator[tuple[str, dict]]:
        if not isinstance(node, dict):
            return
        if isinstance(node.get("items"), list):
            for item in node["items"]:
                if isinstance(item, dict) and isinstance(item.get("fields"), dict):
                    yield from walk(item, f"{path}[]")
                elif isinstance(item, dict):
                    yield f"{path}[]", item
            return
        if isinstance(node.get("fields"), dict):
            for name, child in node["fields"].items():
                yield from walk(child, f"{path}.{name}")
            return
        if "value" in node or "valid" in node:
            yield path, node

    for name, node in (entities or {}).items():
        yield from walk(node, name)


def top_value(entities: dict, name: str) -> Any:
    node = (entities or {}).get(name)
    return node.get("value") if isinstance(node, dict) else None


def first_value(entities: dict, aliases: Iterable[str]) -> tuple[str | None, Any]:
    for alias in aliases:
        value = top_value(entities, alias)
        if not is_empty(value):
            return alias, value
    return None, None


def as_number(value: Any) -> float | None:
    if isinstance(value, bool) or is_empty(value):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value)) if not is_empty(value) else ""


# ---------------------------------------------------------------------------
# Estadística
# ---------------------------------------------------------------------------

def _pct(part: float, whole: float) -> float | None:
    return round(part / whole, 4) if whole else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo), 4)


def describe(values: Iterable[float | None]) -> dict:
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return {"n": 0}
    return {
        "n": len(nums), "mean": round(statistics.fmean(nums), 4), "min": round(min(nums), 4),
        "p10": _quantile(nums, 0.10), "p50": _quantile(nums, 0.50), "p95": _quantile(nums, 0.95),
        "max": round(max(nums), 4), "stdev": round(statistics.pstdev(nums), 4),
        "sum": round(sum(nums), 6),
    }


# ---------------------------------------------------------------------------
# Coherencia interna
# ---------------------------------------------------------------------------

def reference_from_filename(file_name: str) -> dict | None:
    match = FILENAME_REFERENCE_RE.search(Path(file_name).stem)
    if not match:
        return None
    anio, aduana, patente, consecutivo = match.groups()
    return {"anio": anio, "aduana": aduana, "patente": patente, "consecutivo": consecutivo,
            "numero": f"{anio}{aduana}{patente}{consecutivo}"}


def reference_checks(file_name: str, entities: dict, roles: dict[str, str]) -> list[dict]:
    """Coherencia del número de pedimento consigo mismo y con el archivo."""
    checks: list[dict] = []
    numero = _digits(top_value(entities, roles["normalizado"]) or top_value(entities, roles["numero"]))
    parts = {role: _digits(top_value(entities, roles[role])) for role in COMPONENT_WIDTH}
    padded = {role: value.zfill(COMPONENT_WIDTH[role]) if value else "" for role, value in parts.items()}

    if numero and all(padded.values()):
        composed = "".join(padded[r] for r in COMPONENT_WIDTH)
        checks.append({
            "check": "numero_compuesto", "passed": numero == composed,
            "detail": (f"{numero} = año+aduana+patente+consecutivo" if numero == composed
                       else f"{numero} ≠ {composed} (año {padded['anio']}, aduana {padded['aduana']}, "
                            f"patente {padded['patente']}, consecutivo {padded['consecutivo']})"),
        })

    expected = reference_from_filename(file_name)
    if expected:
        mismatches = []
        if numero and numero != expected["numero"]:
            mismatches.append(f"número {numero} ≠ {expected['numero']}")
        for role in COMPONENT_WIDTH:
            if padded[role] and padded[role] != expected[role]:
                mismatches.append(f"{roles[role]} {padded[role]} ≠ {expected[role]}")
        if numero or any(padded.values()):
            checks.append({
                "check": "referencia_archivo", "passed": not mismatches,
                "detail": ("coincide con el nombre del archivo" if not mismatches
                           else "; ".join(mismatches) + " (según el nombre del archivo)"),
            })
        else:
            checks.append({"check": "referencia_archivo", "passed": False,
                           "detail": f"el nombre del archivo trae {expected['numero']} pero no se extrajo ningún componente"})
    return checks


def arithmetic_checks(entities: dict) -> list[dict]:
    out = []
    for rule in ARITHMETIC_CHECKS:
        target_name, target = first_value(entities, rule["target"])
        base_name, base = first_value(entities, rule["operands"][0])
        target_n, base_n = as_number(target), as_number(base)
        if target_n is None or base_n is None:
            continue
        total, used = 0.0, []
        for aliases in rule["operands"]:
            name, value = first_value(entities, aliases)
            number = as_number(value)
            if number is not None:
                total += number
                used.append(f"{name}={number:,.2f}")
        diff = round(target_n - total, 2)
        passed = abs(diff) <= rule["tolerance"]
        out.append({
            "check": rule["name"], "passed": passed, "difference": diff,
            "detail": f"{target_name}={target_n:,.2f} vs {' + '.join(used)} = {total:,.2f} (dif {diff:,.2f}, tol ±{rule['tolerance']:g})",
        })
    return out


# ---------------------------------------------------------------------------
# Análisis principal
# ---------------------------------------------------------------------------

def applicable_expectations(spec: dict | None, doc_type: str) -> tuple[list[dict], list[str]]:
    """Expectativas del ground truth que aplican a `doc_type`, y los tipos que
    declara el archivo cuando ninguna aplica (para explicar por qué se omite)."""
    expectations = (spec or {}).get("expect_documents") or []
    applicable = [e for e in expectations if doc_type in (e.get("type") or [])]
    declared = sorted({t for e in expectations for t in (e.get("type") or [])})
    return applicable, declared


def _degradations(result: dict) -> list[str]:
    signals = []
    if result.get("modelFallbacks"):
        signals.append(f"fallback de modelo ({len(result['modelFallbacks'])})")
    if result.get("segmentationDegraded"):
        signals.append(f"segmentación degradada ({len(result['segmentationDegraded'])})")
    if result.get("truncated"):
        signals.append("páginas truncadas")
    for doc in result.get("documents") or []:
        if (doc.get("pageSelection") or {}).get("failOpen"):
            signals.append(f"selección de páginas en fail-open ({doc.get('documentType')})")
    for warning in result.get("warnings") or []:
        signals.append(f"warning: {str(warning)[:120]}")
    return signals


def analyze(cases: list[dict], doc_type: str, type_spec: dict | None, *,
            env: str | None = None, docs_dir: str | None = None,
            min_approval_rate: float | None = None, min_field_index: float | None = None) -> dict:
    """Cada caso: `{"file", "summary": <resumen del E2E>, "result": <S3 completo o None>}`."""
    min_conf = float((type_spec or {}).get("min_confidence", 0.70))
    fields_cat = catalog_fields(type_spec)
    roles = pedimento_roles(type_spec)
    # Los chequeos del número de pedimento solo aplican a tipos que declaran
    # sus componentes; en una CURP o un CFDI, 2-2-4-7 dígitos en el nombre
    # del archivo no significan nada.
    has_pedimento_reference = any(roles[r] in fields_cat for r in ("numero", "normalizado", "consecutivo"))
    local_version = (type_spec or {}).get("schema_version")

    field_acc: dict[str, dict] = defaultdict(lambda: {
        "occurrences": 0, "present": 0, "invalid": 0, "lowConfidence": 0,
        "applicableConf": [], "presentConf": [], "sources": Counter(), "reasons": Counter(),
        "values": Counter(), "invalidFiles": set(),
    })
    array_rows: dict[str, list[int]] = defaultdict(list)
    rule_acc: dict[tuple[str, str], dict] = {}
    per_file, documents_rows = [], []
    coherence = {"reference": [], "arithmetic": []}
    status_counts, business_counts, other_types = Counter(), Counter(), Counter()
    deployed_versions: set[str] = set()
    doc_confidences, e2e_seconds, pipeline_seconds, costs = [], [], [], []
    pages_total = pages_ocr = pages_considered = pages_extracted = 0
    cross_failed = 0

    for case in cases:
        name, summary, result = case["file"], case.get("summary") or {}, case.get("result") or {}
        docs = result.get("documents") or []
        mine = [d for d in docs if d.get("documentType") == doc_type]
        for d in docs:
            if d.get("documentType") != doc_type:
                other_types[d.get("documentType") or "?"] += 1
        pages = result.get("pages") or {}
        pages_total += int(pages.get("totalPages") or 0)
        pages_ocr += int(pages.get("ocrPages") or 0)
        selection = (result.get("usage") or {}).get("pageSelection") or {}
        pages_considered += int(selection.get("pagesConsidered") or 0)
        pages_extracted += int(selection.get("pagesExtracted") or 0)
        version = (((result.get("tracking") or {}).get("schema") or {}).get("typeVersions") or {}).get(doc_type)
        if version:
            deployed_versions.add(str(version))
        if summary.get("durationSeconds") is not None:
            e2e_seconds.append(float(summary["durationSeconds"]))
        if result.get("processingSeconds") is not None:
            pipeline_seconds.append(float(result["processingSeconds"]))
        if summary.get("costUsd") is not None:
            costs.append(float(summary["costUsd"]))
        if summary.get("businessStatus"):
            business_counts[summary["businessStatus"]] += 1
        cross_failed += sum(1 for c in result.get("crossDocumentChecks") or []
                            if not c.get("valid") and not c.get("skipped") and c.get("severity") == "error")

        file_invalid = file_low = file_failed_rules = 0
        file_reference_ok: bool | None = None
        file_arithmetic_ok: bool | None = None
        file_values: dict[str, Any] = {}
        for index, doc in enumerate(mine):
            status = doc.get("status") or "?"
            status_counts[status] += 1
            entities = doc.get("extractedEntities") or {}
            if doc.get("confidence") is not None:
                doc_confidences.append((float(doc["confidence"]), name))

            leaves_by_path: dict[str, list[dict]] = defaultdict(list)
            for path, leaf in iter_leaves(entities):
                leaves_by_path[path].append(leaf)
            scalar_paths = {p for p, meta in fields_cat.items()
                            if "[]" not in p and meta["type"] not in ("object", "array")}
            doc_invalid = doc_low = 0
            for path in sorted(scalar_paths | set(leaves_by_path)):
                leaves = leaves_by_path.get(path, [])
                acc = field_acc[path]
                if "[]" not in path:
                    acc["occurrences"] += 1  # un campo escalar aplica a todo documento del tipo
                else:
                    acc["occurrences"] += len(leaves)
                for leaf in leaves:
                    value = leaf.get("value")
                    present, valid = not is_empty(value), leaf.get("valid") is not False
                    confidence = float(leaf.get("confidence") or 0.0)
                    if present:
                        acc["present"] += 1
                        acc["presentConf"].append(confidence)
                        acc["sources"][leaf.get("source") or "MODEL_EXTRACTION"] += 1
                        if not isinstance(value, (dict, list)):
                            acc["values"][json.dumps(value, ensure_ascii=False)] += 1
                        if confidence < min_conf:
                            acc["lowConfidence"] += 1
                            doc_low += 1
                    if not valid:
                        acc["invalid"] += 1
                        acc["invalidFiles"].add(name)
                        acc["reasons"][str(leaf.get("reason") or "inválido sin razón")[:160]] += 1
                        doc_invalid += 1
                    if present or not valid:
                        acc["applicableConf"].append(confidence if valid else min(confidence, min_conf))
                    if index == 0 and "[]" not in path and present and not isinstance(value, (dict, list)):
                        file_values[path] = value

            for path, meta in fields_cat.items():
                if meta["type"] == "array" and "[]" not in path:
                    node: Any = entities
                    for i, part in enumerate(path.split(".")):
                        node = (node.get(part) if i == 0 else (node.get("fields") or {}).get(part)) if isinstance(node, dict) else None
                    if isinstance(node, dict) and isinstance(node.get("items"), list):
                        array_rows[path].append(len(node["items"]))

            failed_rules = 0
            for check in doc.get("checks") or []:
                key = (check.get("rule") or "?", norm_path(check.get("field") or ""))
                acc = rule_acc.setdefault(key, {"rule": key[0], "field": key[1], "evaluated": 0, "failed": 0,
                                               "skipped": 0, "severities": Counter(), "files": set(), "messages": Counter()})
                acc["evaluated"] += 1
                acc["severities"][check.get("severity") or "?"] += 1
                if check.get("skipped"):
                    acc["skipped"] += 1
                    acc["messages"][str(check.get("message") or "")[:160]] += 1
                elif not check.get("valid"):
                    acc["failed"] += 1
                    acc["files"].add(name)
                    acc["messages"][str(check.get("message") or "")[:160]] += 1
                    if check.get("severity") in ("error", "warning"):
                        failed_rules += 1

            refs = reference_checks(name, entities, roles) if has_pedimento_reference else []
            for ref in refs:
                coherence["reference"].append({"file": name, "status": status, **ref})
            if refs:
                ok = all(r["passed"] for r in refs)
                file_reference_ok = ok if file_reference_ok is None else (file_reference_ok and ok)
            arith = arithmetic_checks(entities)
            for check in arith:
                coherence["arithmetic"].append({"file": name, "status": status, **check})
            if arith:
                ok = all(c["passed"] for c in arith)
                file_arithmetic_ok = ok if file_arithmetic_ok is None else (file_arithmetic_ok and ok)

            doc_index = describe(
                min(float(leaf.get("confidence") or 0), min_conf) if leaf.get("valid") is False else float(leaf.get("confidence") or 0)
                for leaves in leaves_by_path.values() for leaf in leaves
                if not is_empty(leaf.get("value")) or leaf.get("valid") is False
            ).get("mean")
            documents_rows.append({
                "file": name, "pages": f"{doc.get('pageStart')}-{doc.get('pageEnd')}", "status": status,
                "confidence": doc.get("confidence"), "invalidFields": doc_invalid, "lowConfidenceFields": doc_low,
                "failedRules": failed_rules, "index": round(doc_index * 100, 1) if doc_index is not None else None,
            })
            file_invalid += doc_invalid
            file_low += doc_low
            file_failed_rules += failed_rules

        gt = summary.get("groundTruthPassed")
        per_file.append({
            "file": name, "documentId": summary.get("documentId"), "passed": summary.get("passed"),
            "infraPassed": summary.get("infraPassed"), "executionStatus": summary.get("executionStatus"),
            "groundTruth": gt, "groundTruthNote": summary.get("groundTruthNote"),
            "businessStatus": summary.get("businessStatus"), "classification": summary.get("documentClassification"),
            "statuses": [d.get("status") for d in mine], "documentsOfType": len(mine),
            "otherSegments": len(docs) - len(mine), "pages": pages.get("totalPages"), "ocrPages": pages.get("ocrPages"),
            "costUsd": summary.get("costUsd"), "durationSeconds": summary.get("durationSeconds"),
            "costEstimate": summary.get("costEstimate"), "costBreakdown": summary.get("costBreakdown"),
            "processingSeconds": result.get("processingSeconds"),
            "confidence": mine[0].get("confidence") if mine else None,
            "invalidFields": file_invalid, "lowConfidenceFields": file_low, "failedRules": file_failed_rules,
            "referenceOk": file_reference_ok, "arithmeticOk": file_arithmetic_ok,
            "degradations": _degradations(result), "error": summary.get("error"),
            "values": file_values,
        })

    # --- parámetros ---------------------------------------------------------
    field_rows = []
    for path, acc in field_acc.items():
        meta = fields_cat.get(path, {"type": None, "required": False, "computed": False})
        conf = describe(acc["presentConf"])
        applicable = acc["applicableConf"]
        index = round(statistics.fmean(applicable) * 100, 1) if applicable else None
        distinct = len(acc["values"])
        # Importes y fechas casi nunca se repiten entre documentos distintos:
        # solo códigos/booleanos (y derivados, p. ej. Tipo_Pago) son categóricos.
        categorical = (acc["present"] >= 3 and 1 <= distinct <= 5 and distinct < acc["present"]
                       and (meta.get("type") in ("string", "boolean") or meta.get("computed")))
        field_rows.append({
            "path": path, "type": meta.get("type"), "required": meta.get("required", False),
            "computed": meta.get("computed", False), "inCatalog": path in fields_cat,
            "occurrences": acc["occurrences"], "present": acc["present"],
            "fillRate": _pct(acc["present"], acc["occurrences"]),
            "invalid": acc["invalid"], "validRate": _pct(acc["occurrences"] - acc["invalid"], acc["occurrences"]),
            "lowConfidence": acc["lowConfidence"], "confidence": conf, "index": index,
            "sources": dict(acc["sources"].most_common()), "topReasons": acc["reasons"].most_common(3),
            "invalidFiles": sorted(acc["invalidFiles"]), "distinctValues": distinct,
            "valueDistribution": ([(json.loads(v), n) for v, n in acc["values"].most_common()] if categorical else None),
        })
    field_rows.sort(key=lambda r: (r["index"] is None, r["index"] if r["index"] is not None else 0,
                                   -int(r["required"]), r["path"]))
    lowest = [r for r in field_rows if r["index"] is not None][:10]
    never = [r["path"] for r in field_rows if r["occurrences"] and r["present"] == 0 and r["invalid"] == 0
             and "[]" not in r["path"]]
    flat = [r["path"] for r in field_rows if r["present"] >= 3 and r["confidence"].get("stdev") == 0]

    arrays = []
    for path, counts in sorted(array_rows.items()):
        d = describe(counts)
        arrays.append({"path": path, "documents": len(counts), "rowsMin": d.get("min"), "rowsMean": d.get("mean"),
                       "rowsMax": d.get("max"), "emptyDocuments": sum(1 for c in counts if c == 0)})

    rules = []
    for acc in rule_acc.values():
        rules.append({
            "rule": acc["rule"], "field": acc["field"], "evaluated": acc["evaluated"], "failed": acc["failed"],
            "skipped": acc["skipped"], "severity": acc["severities"].most_common(1)[0][0] if acc["severities"] else None,
            "files": sorted(acc["files"]), "topMessage": acc["messages"].most_common(1)[0][0] if acc["messages"] else None,
        })
    failing_rules = sorted([r for r in rules if r["failed"]], key=lambda r: (-r["failed"], r["rule"], r["field"]))
    inert_rules = sorted([r for r in rules if r["skipped"]], key=lambda r: (r["rule"], r["field"]))

    # --- documentos y ejecución ---------------------------------------------
    n_docs = sum(status_counts.values())
    conf_values = [c for c, _ in doc_confidences]
    lowest_doc = min(doc_confidences) if doc_confidences else None
    worst_documents = sorted(
        documents_rows,
        key=lambda r: (r["status"] == APPROVED, -(r["failedRules"] + r["invalidFields"]),
                       r["index"] if r["index"] is not None else 101),
    )[:10]

    ref_rows, arith_rows = coherence["reference"], coherence["arithmetic"]
    coherence_summary = {
        "numeroCompuesto": _count(r for r in ref_rows if r["check"] == "numero_compuesto"),
        "referenciaArchivo": _count(r for r in ref_rows if r["check"] == "referencia_archivo"),
        "aritmetica": _count(arith_rows),
        "failures": [r for r in ref_rows + arith_rows if not r["passed"]],
    }

    gt_values = [f["groundTruth"] for f in per_file]
    execution = {
        "files": len(per_file),
        "infraPassed": sum(1 for f in per_file if f["infraPassed"]),
        "stepFunctionsSucceeded": sum(1 for f in per_file if f["executionStatus"] == "SUCCEEDED"),
        "groundTruthEvaluated": sum(1 for g in gt_values if g is not None),
        "groundTruthPassed": sum(1 for g in gt_values if g is True),
        "e2eSeconds": describe(e2e_seconds), "pipelineSeconds": describe(pipeline_seconds),
        "costUsd": describe(costs), "costPerPageUsd": round(sum(costs) / pages_total, 5) if costs and pages_total else None,
        "mostExpensive": max(((f["costUsd"], f["file"]) for f in per_file if f["costUsd"] is not None), default=None),
        "slowest": max(((f["durationSeconds"], f["file"]) for f in per_file if f["durationSeconds"] is not None), default=None),
        "pages": pages_total, "ocrPages": pages_ocr,
        "pageSelection": {"pagesConsidered": pages_considered, "pagesExtracted": pages_extracted} if pages_considered else None,
        "crossDocumentErrors": cross_failed,
        "costEstimate": cost_estimate_accuracy(per_file),
    }
    documents = {
        "total": n_docs, "otherSegments": dict(other_types),
        "byStatus": {s: status_counts[s] for s in STATUS_ORDER if status_counts[s]} | {
            s: n for s, n in status_counts.items() if s not in STATUS_ORDER},
        "approvalRate": _pct(status_counts[APPROVED], n_docs),
        "businessStatus": dict(business_counts),
        "confidence": describe(conf_values),
        "lowestConfidence": {"confidence": lowest_doc[0], "file": lowest_doc[1]} if lowest_doc else None,
    }

    analysis = {
        "schemaVersion": 1, "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "docType": doc_type, "environment": env, "docsDir": docs_dir,
        "catalog": {"found": type_spec is not None, "localSchemaVersion": local_version,
                    "deployedTypeVersions": sorted(deployed_versions), "minConfidence": min_conf,
                    "drift": bool(local_version and deployed_versions and deployed_versions != {str(local_version)})},
        "execution": execution, "documents": documents,
        "fields": field_rows, "lowestFields": lowest, "neverExtracted": never, "flatConfidenceFields": flat,
        "arrays": arrays, "failingRules": failing_rules, "inertRules": inert_rules,
        "coherence": coherence_summary, "worstDocuments": worst_documents, "perFile": per_file,
    }
    analysis["verdict"] = verdict(analysis, min_approval_rate=min_approval_rate, min_field_index=min_field_index)
    return analysis


def cost_estimate_accuracy(per_file: list[dict]) -> dict | None:
    """Cuánto se desvió el costo estimado antes de correr (plan) del que la
    auditoría registró para cada archivo — la validación del estimador que se
    repite en cada corrida, con el mismo catálogo y el mismo modelo."""
    rows = [f for f in per_file if (f.get("costEstimate") or {}).get("totalUsd") is not None and f.get("costUsd")]
    if not rows:
        return None
    est = sum(f["costEstimate"]["totalUsd"] for f in rows)
    real = sum(f["costUsd"] for f in rows)
    errors = [(f["costEstimate"]["totalUsd"] - f["costUsd"]) / f["costUsd"] for f in rows]
    by_service = {}
    for key in ("textractUsd", "bedrockUsd", "lambdaUsd", "inputTokens", "outputTokens"):
        pairs = [(f["costEstimate"].get(key), (f.get("costBreakdown") or {}).get(key)) for f in rows]
        pairs = [(e, r) for e, r in pairs if e is not None and r is not None]
        if pairs:
            by_service[key] = {"estimated": round(sum(e for e, _ in pairs), 6), "real": round(sum(r for _, r in pairs), 6)}
    return {
        "files": len(rows), "estimatedUsd": round(est, 6), "realUsd": round(real, 6),
        "aggregateErr": round((est - real) / real, 4) if real else None,
        "medianAbsErr": round(statistics.median(abs(e) for e in errors), 4),
        "withinCeiling": sum(1 for f in rows if f["costUsd"] <= (f["costEstimate"].get("totalHighUsd") or 0) * 1.2),
        "byService": by_service,
        "worst": max(((abs(e), f["file"], e) for e, f in zip(errors, rows)), default=None),
    }


def _count(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    return {"evaluated": len(rows), "passed": sum(1 for r in rows if r["passed"])}


def verdict(analysis: dict, *, min_approval_rate: float | None = None, min_field_index: float | None = None) -> dict:
    """NO_CONFIABLE bloquea (exit 1); CON_RESERVAS se reporta pero no falla.

    Lo peor que puede hacer la herramienta no es mandar a revisión: es APROBAR
    un valor equivocado, porque ese llega al ERP sin que nadie lo mire. Por
    eso una incoherencia de referencia en un documento APPROVED bloquea, y la
    misma incoherencia en uno que ya quedó en revisión solo es reserva.
    """
    blockers, reservations, notes = [], [], []
    ex, docs = analysis["execution"], analysis["documents"]
    min_conf_index = analysis["catalog"]["minConfidence"] * 100

    if ex["files"] == 0:
        blockers.append("no se procesó ningún archivo")
    if ex["infraPassed"] < ex["files"]:
        blockers.append(f"{ex['files'] - ex['infraPassed']} archivo(s) fallaron en infraestructura")
    if ex["groundTruthPassed"] < ex["groundTruthEvaluated"]:
        blockers.append(f"{ex['groundTruthEvaluated'] - ex['groundTruthPassed']} archivo(s) no cumplen su ground truth")
    if ex["files"] and docs["total"] == 0:
        blockers.append(f"ningún archivo produjo un documento {analysis['docType']}")
    for failure in analysis["coherence"]["failures"]:
        target = blockers if (failure["status"] == APPROVED and failure["check"] in ("numero_compuesto", "referencia_archivo")) else reservations
        target.append(f"{failure['file']} ({failure['status']}): {failure['detail']}")

    if min_approval_rate is not None and (docs["approvalRate"] or 0) < min_approval_rate:
        blockers.append(f"tasa de aprobación {docs['approvalRate'] or 0:.0%} < mínimo {min_approval_rate:.0%}")
    for row in analysis["fields"]:
        if not row["required"] or row["index"] is None:
            continue
        if min_field_index is not None and row["index"] < min_field_index:
            blockers.append(f"campo obligatorio {row['path']} con índice {row['index']:.1f} < mínimo {min_field_index:g}")
        elif row["index"] < min_conf_index:
            reservations.append(f"campo obligatorio {row['path']} con índice {row['index']:.1f} (< {min_conf_index:g})")

    if analysis["inertRules"]:
        reservations.append(f"{len(analysis['inertRules'])} regla(s) declaradas en el catálogo que no se ejecutan (skipped): "
                            + ", ".join(sorted({f"{r['rule']}@{r['field']}" for r in analysis['inertRules']})))
    degraded = [f["file"] for f in analysis["perFile"] if f["degradations"]]
    if degraded:
        reservations.append(f"{len(degraded)} archivo(s) con señales de degradación: {', '.join(degraded)}")
    if analysis["catalog"]["drift"]:
        reservations.append(f"catálogo desplegado {analysis['catalog']['deployedTypeVersions']} ≠ local "
                            f"{analysis['catalog']['localSchemaVersion']}")
    if ex["crossDocumentErrors"]:
        reservations.append(f"{ex['crossDocumentErrors']} regla(s) cruzadas entre documentos con error")

    uncovered = ex["files"] - ex["groundTruthEvaluated"]
    if uncovered:
        notes.append(f"{uncovered} de {ex['files']} archivo(s) sin ground truth para {analysis['docType']}: la exactitud "
                     "de sus valores descansa en las reglas, la coherencia interna y la confianza auto-reportada")
    if analysis["flatConfidenceFields"]:
        notes.append(f"{len(analysis['flatConfidenceFields'])} campo(s) con confianza idéntica en todas sus lecturas: "
                     "ahí la confianza no discrimina, mira validez y coherencia")
    cost = ex.get("costEstimate") or {}
    if cost.get("aggregateErr") is not None and abs(cost["aggregateErr"]) > 0.5:
        notes.append(f"el costo real se desvió {cost['aggregateErr'] * 100:+.0f}% del estimado del plan: "
                     "corre `pnpm costos:validar -- --write` para recalibrar el estimador")

    level = "NO_CONFIABLE" if blockers else ("CON_RESERVAS" if reservations else "CONFIABLE")
    return {"level": level, "blockers": blockers, "reservations": reservations, "notes": notes}


def compare_runs(current: dict, previous: dict | None) -> dict | None:
    """Diferencias contra la corrida anterior del mismo tipo: estados que
    cambiaron, valores que cambiaron (no-determinismo o regresión) e índices
    de campo que se movieron 5 puntos o más."""
    if not previous:
        return None
    before = {f["file"]: f for f in previous.get("perFile") or []}
    status_changes, value_changes = [], []
    for row in current.get("perFile") or []:
        old = before.get(row["file"])
        if not old:
            continue
        if (old.get("statuses"), old.get("businessStatus")) != (row.get("statuses"), row.get("businessStatus")):
            status_changes.append({"file": row["file"], "before": old.get("statuses"), "after": row.get("statuses"),
                                   "businessBefore": old.get("businessStatus"), "businessAfter": row.get("businessStatus")})
        old_values, new_values = old.get("values") or {}, row.get("values") or {}
        for field in sorted(set(old_values) | set(new_values)):
            if old_values.get(field) != new_values.get(field):
                value_changes.append({"file": row["file"], "field": field,
                                      "before": old_values.get(field), "after": new_values.get(field)})
    old_index = {f["path"]: f.get("index") for f in previous.get("fields") or []}
    index_changes = []
    for row in current.get("fields") or []:
        prev = old_index.get(row["path"])
        if prev is not None and row["index"] is not None and abs(row["index"] - prev) >= 5:
            index_changes.append({"path": row["path"], "before": prev, "after": row["index"]})
    return {"previousGeneratedAt": previous.get("generatedAt"), "statusChanges": status_changes,
            "valueChanges": value_changes, "indexChanges": index_changes,
            "approvalRateBefore": (previous.get("documents") or {}).get("approvalRate"),
            "approvalRateAfter": (current.get("documents") or {}).get("approvalRate")}


def apply_comparison(analysis: dict, comparison: dict | None) -> None:
    """Adjunta la comparación y la hace pesar en el veredicto: el mismo archivo
    con otro estado u otro valor que la corrida anterior es no-determinismo o
    regresión, y ninguno de los dos se ve mirando una sola corrida."""
    analysis["comparison"] = comparison
    if not comparison:
        return
    v = analysis["verdict"]
    if comparison["statusChanges"]:
        v["reservations"].append(f"{len(comparison['statusChanges'])} archivo(s) cambiaron de estado contra la corrida anterior")
    if comparison["valueChanges"]:
        files = sorted({c["file"] for c in comparison["valueChanges"]})
        v["reservations"].append(f"{len(comparison['valueChanges'])} valor(es) distintos a la corrida anterior en "
                                 f"{len(files)} archivo(s)")
    if v["level"] == "CONFIABLE" and v["reservations"]:
        v["level"] = "CON_RESERVAS"


def load_analysis(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def find_previous_analysis(history_env_dir: Path, doc_type: str, docs_dir_name: str,
                           exclude: Path | None = None) -> dict | None:
    """La corrida más reciente del mismo tipo sobre una carpeta del mismo
    nombre — de `test:e2e:pedimento` (`<run>/analisis.json`) o de la suite
    (`<run>/<TIPO>/analisis.json`); los runIds empiezan con timestamp."""
    if not history_env_dir.exists():
        return None
    candidates = sorted(history_env_dir.glob("*/analisis.json")) + sorted(history_env_dir.glob("*/*/analisis.json"))
    for path in sorted(candidates, key=lambda p: p.relative_to(history_env_dir).parts[0], reverse=True):
        if exclude and exclude in path.parents:
            continue
        data = load_analysis(path)
        if data and data.get("docType") == doc_type and Path(data.get("docsDir") or "").name == docs_dir_name:
            data["_path"] = str(path)
            return data
    return None


def run_summary(analysis: dict, *, estimate: dict | None = None, duration_seconds: float | None = None) -> dict:
    """Resumen para `run.json` con las llaves que `quality_history.quality_snapshot`
    ya grafica (tests/failures/e2ePassRate/costUsd/durationSeconds)."""
    ex, docs = analysis["execution"], analysis["documents"]
    failures = sum(1 for f in analysis["perFile"] if not f["passed"])
    required = [f for f in analysis["fields"] if f["required"] and f["index"] is not None]
    return {
        "docType": analysis["docType"], "verdict": analysis["verdict"]["level"],
        "tests": ex["files"], "failures": failures,
        "e2ePassRate": _pct(ex["files"] - failures, ex["files"]),
        "approvalRate": docs["approvalRate"], "documents": docs["total"], "byStatus": docs["byStatus"],
        "requiredFieldIndexMin": min((f["index"] for f in required), default=None),
        "lowestField": (analysis["lowestFields"][0]["path"] if analysis["lowestFields"] else None),
        "costUsd": ex["costUsd"].get("sum"), "estimatedCostUsd": (estimate or {}).get("estimateUsd"),
        "durationSeconds": round(duration_seconds, 3) if duration_seconds is not None else None,
    }


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

GREEN, RED, YELLOW, CYAN, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[1m", "\033[0m"


def _fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _fmt_num(x: float | None, digits: int = 2) -> str:
    return "—" if x is None else f"{x:.{digits}f}"


def _fmt_money(x: float | None) -> str:
    return "—" if x is None else f"${x:,.4f}" if x < 0.1 else f"${x:,.2f}"


def _clip(value: Any, width: int = 40) -> str:
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def _cost_estimate_lines(ce: dict | None) -> list[str]:
    if not ce:
        return []
    labels = {"textractUsd": "Textract", "bedrockUsd": "Bedrock", "lambdaUsd": "Lambda"}
    parts = [f"{label} {_fmt_money(ce['byService'][k]['estimated'])}→{_fmt_money(ce['byService'][k]['real'])}"
             for k, label in labels.items() if k in ce["byService"]]
    tokens = ce["byService"].get("inputTokens"), ce["byService"].get("outputTokens")
    lines = [f"Estimado     {_fmt_money(ce['estimatedUsd'])} → real {_fmt_money(ce['realUsd'])} "
             f"({ce['aggregateErr'] * 100:+.0f}% total, ±{ce['medianAbsErr'] * 100:.0f}% mediano por archivo; "
             f"{ce['withinCeiling']}/{ce['files']} dentro del tope)"]
    if parts:
        lines.append("             " + " · ".join(parts))
    if all(tokens):
        lines.append(f"             tokens entrada {tokens[0]['estimated']:,.0f}→{tokens[0]['real']:,.0f} · "
                     f"salida {tokens[1]['estimated']:,.0f}→{tokens[1]['real']:,.0f}")
    return lines


def _tri(value: bool | None, yes: str = "✓", no: str = "✗") -> str:
    return "—" if value is None else (yes if value else no)


def _bar(fraction: float | None, width: int = 20) -> str:
    fraction = max(0.0, min(1.0, fraction or 0.0))
    full = round(fraction * width)
    return "█" * full + "░" * (width - full)


def _index_color(index: float | None, threshold: float) -> str:
    if index is None:
        return DIM
    return GREEN if index >= 95 else YELLOW if index >= threshold else RED


def _short_sources(sources: dict) -> str:
    alias = {"MODEL_EXTRACTION": "modelo", "QR_RECOVERY": "QR", "DERIVED": "derivado", "NORMALIZED": "normalizado"}
    return ", ".join(f"{alias.get(k, k)} {v}" for k, v in sources.items()) or "—"


def render_text(a: dict, *, color: bool = True, field_limit: int | None = None) -> str:
    c = (lambda code: code) if color else (lambda code: "")
    lines: list[str] = []
    out = lines.append
    ex, docs, cat = a["execution"], a["documents"], a["catalog"]
    threshold = cat["minConfidence"] * 100
    level = a["verdict"]["level"]
    level_color = {"CONFIABLE": GREEN, "CON_RESERVAS": YELLOW, "NO_CONFIABLE": RED}[level]

    out(f"\n{c(CYAN)}{'═' * 92}{c(RESET)}")
    out(f"{c(BOLD)}ANÁLISIS DEL DATASET · {a['docType']} · {a.get('environment') or '-'} · {ex['files']} archivo(s){c(RESET)}")
    out(f"{c(CYAN)}{'═' * 92}{c(RESET)}")
    out(f"Veredicto: {c(level_color)}{c(BOLD)}{level.replace('_', ' ')}{c(RESET)}")
    for text in a["verdict"]["blockers"]:
        out(f"  {c(RED)}✗ {text}{c(RESET)}")
    for text in a["verdict"]["reservations"]:
        out(f"  {c(YELLOW)}! {text}{c(RESET)}")
    for text in a["verdict"]["notes"]:
        out(f"  {c(DIM)}· {text}{c(RESET)}")

    out(f"\n{c(BOLD)}1. Ejecución{c(RESET)}")
    out(f"   Infra OK {ex['infraPassed']}/{ex['files']} · Procesamiento SUCCEEDED {ex['stepFunctionsSucceeded']}/{ex['files']}"
        f" · ground truth {ex['groundTruthPassed']}/{ex['groundTruthEvaluated']} evaluados")
    e2e, pipe = ex["e2eSeconds"], ex["pipelineSeconds"]
    if e2e.get("n"):
        out(f"   Tiempo E2E   p50 {e2e['p50']:.0f}s · p95 {e2e['p95']:.0f}s · máx {e2e['max']:.0f}s"
            + (f" ({ex['slowest'][1]})" if ex.get("slowest") else ""))
    if pipe.get("n"):
        out(f"   Pipeline     p50 {pipe['p50']:.0f}s · p95 {pipe['p95']:.0f}s · máx {pipe['max']:.0f}s")
    cost = ex["costUsd"]
    if cost.get("n"):
        out(f"   Costo        {_fmt_money(cost['sum'])} total · {_fmt_money(cost['mean'])}/archivo · "
            f"{_fmt_money(ex['costPerPageUsd'])}/página · más caro {_fmt_money(ex['mostExpensive'][0])} ({ex['mostExpensive'][1]})")
    for line in _cost_estimate_lines(ex.get("costEstimate")):
        out(f"   {line}")
    sel = ex.get("pageSelection")
    out(f"   Páginas      {ex['pages']} (OCR {ex['ocrPages']})"
        + (f" · selección: {sel['pagesExtracted']}/{sel['pagesConsidered']} extraídas" if sel else ""))
    if cat["deployedTypeVersions"]:
        drift = f" {c(YELLOW)}≠ local {cat['localSchemaVersion']}{c(RESET)}" if cat["drift"] else " (= local)"
        out(f"   Catálogo     desplegado {', '.join(cat['deployedTypeVersions'])}{drift}")

    out(f"\n{c(BOLD)}2. Resultado de negocio{c(RESET)}")
    others = ", ".join(f"{k} {v}" for k, v in docs["otherSegments"].items())
    out(f"   Documentos {a['docType']}: {docs['total']}" + (f"  {c(DIM)}(otros segmentos: {others}){c(RESET)}" if others else ""))
    for status, n in docs["byStatus"].items():
        colr = GREEN if status == APPROVED else RED if status in ("REJECTED", "PROCESSING_ERROR") else YELLOW
        out(f"   {c(colr)}{status:<20}{c(RESET)} {n:>3}  {_bar(n / docs['total'] if docs['total'] else 0)} {_fmt_pct(n / docs['total'] if docs['total'] else None)}")
    if docs["businessStatus"]:
        out("   Validación pública: " + " · ".join(f"{k} {v}" for k, v in docs["businessStatus"].items()))
    dc = docs["confidence"]
    if dc.get("n"):
        out(f"   Confianza por documento: media {dc['mean']:.3f} · p50 {dc['p50']:.3f} · mín {dc['min']:.3f}"
            + (f" ({docs['lowestConfidence']['file']})" if docs.get("lowestConfidence") else ""))

    out(f"\n{c(BOLD)}3. Confianza por parámetro{c(RESET)}  {c(DIM)}índice = confianza media donde el campo aplica "
        f"(presente o inválido; obligatorio faltante = 0; inválido topado a {threshold:g}){c(RESET)}")
    header = f"   {'Campo':<48}{'Req':>4}{'Llenado':>9}{'Válido':>8}{'Conf.μ':>8}{'Conf.mín':>9}{'<mín':>6}{'Índice':>8}  Origen"
    out(c(DIM) + header + c(RESET))
    rows = a["fields"] if field_limit is None else a["fields"][:field_limit]
    for r in rows:
        conf = r["confidence"]
        idx = "—" if r["index"] is None else f"{r['index']:.1f}"
        out(f"   {r['path'][:47]:<48}{'sí' if r['required'] else '':>4}"
            f"{_fmt_pct(r['fillRate']):>9}{_fmt_pct(r['validRate']):>8}{_fmt_num(conf.get('mean')):>8}"
            f"{_fmt_num(conf.get('min')):>9}{r['lowConfidence'] or '':>6}"
            f"{c(_index_color(r['index'], threshold))}{idx:>8}{c(RESET)}  {c(DIM)}{_short_sources(r['sources'])}{c(RESET)}")
    if field_limit is not None and len(a["fields"]) > field_limit:
        out(f"   {c(DIM)}… {len(a['fields']) - field_limit} campo(s) más en analisis.md{c(RESET)}")

    out(f"\n{c(BOLD)}4. Parámetros con índice más bajo{c(RESET)}")
    if not a["lowestFields"]:
        out(f"   {c(DIM)}(sin datos){c(RESET)}")
    for r in a["lowestFields"]:
        detail = f"llenado {_fmt_pct(r['fillRate'])} · válido {_fmt_pct(r['validRate'])} · conf. mín {_fmt_num(r['confidence'].get('min'))}"
        if r["lowConfidence"]:
            detail += f" · {r['lowConfidence']} lectura(s) bajo {cat['minConfidence']}"
        reasons = "; ".join(f"{text} ×{n}" for text, n in r["topReasons"])
        out(f"   {c(_index_color(r['index'], threshold))}{r['index']:>5.1f}{c(RESET)}  {r['path']}"
            f"{' (obligatorio)' if r['required'] else ''} — {detail}" + (f" — {reasons}" if reasons else ""))
        if r["invalidFiles"]:
            out(f"          {c(DIM)}archivos: {', '.join(r['invalidFiles'])}{c(RESET)}")
    if a["neverExtracted"]:
        out(f"   {c(DIM)}Nunca extraídos (opcionales, vacíos en todo el dataset): {', '.join(a['neverExtracted'])}{c(RESET)}")

    if a["arrays"]:
        out(f"\n{c(BOLD)}5. Arreglos (filas por documento){c(RESET)}")
        for r in a["arrays"]:
            out(f"   {r['path']:<32} docs {r['documents']} · filas mín {r['rowsMin']:.0f} / media {r['rowsMean']:.1f}"
                f" / máx {r['rowsMax']:.0f} · vacíos {r['emptyDocuments']}")

    out(f"\n{c(BOLD)}6. Reglas del motor{c(RESET)}")
    if not a["failingRules"]:
        out(f"   {c(GREEN)}✓ ninguna regla falló en el dataset{c(RESET)}")
    for r in a["failingRules"][:15]:
        colr = RED if r["severity"] == "error" else YELLOW
        out(f"   {c(colr)}✗{c(RESET)} {r['rule']:<24} {r['field'][:40]:<40} {r['failed']}/{r['evaluated']}  {c(DIM)}{r['topMessage'] or ''}{c(RESET)}")
    if a["inertRules"]:
        out(f"   {c(YELLOW)}Reglas inertes (declaradas, nunca se ejecutan):{c(RESET)}")
        for r in a["inertRules"]:
            out(f"     · {r['rule']} en {r['field']}  {c(DIM)}{r['topMessage'] or ''}{c(RESET)}")

    out(f"\n{c(BOLD)}7. Coherencia interna (no requiere ground truth){c(RESET)}")
    coh = a["coherence"]
    for label, key in (("Número = año+aduana+patente+consecutivo", "numeroCompuesto"),
                       ("Referencia del nombre de archivo vs extraída", "referenciaArchivo"),
                       ("Cuadres aritméticos", "aritmetica")):
        cnt = coh[key]
        if not cnt["evaluated"]:
            out(f"   {c(DIM)}– {label}: no aplicó a ningún documento{c(RESET)}")
            continue
        ok = cnt["passed"] == cnt["evaluated"]
        out(f"   {c(GREEN if ok else RED)}{'✓' if ok else '✗'}{c(RESET)} {label}: {cnt['passed']}/{cnt['evaluated']}")
    for failure in coh["failures"]:
        out(f"     {c(RED)}✗ {failure['file']} [{failure['status']}]{c(RESET)} {failure['detail']}")

    categorical = [r for r in a["fields"] if r["valueDistribution"]]
    if categorical:
        out(f"\n{c(BOLD)}8. Distribución de valores (campos categóricos){c(RESET)}")
        for r in categorical:
            out(f"   {r['path']:<48} " + " · ".join(f"{_clip(v)}×{n}" for v, n in r["valueDistribution"]))

    out(f"\n{c(BOLD)}9. Por archivo{c(RESET)}")
    out(c(DIM) + f"   {'':2}{'Archivo':<40}{'Estado':<18}{'Negocio':<19}{'Conf':>6}{'Inv':>5}{'Regl':>5}{'Ref':>5}{'Págs':>6}{'Seg':>6}{'Costo':>9}" + c(RESET))
    for f in a["perFile"]:
        mark = f"{c(GREEN)}✓{c(RESET)}" if f["passed"] else f"{c(RED)}✗{c(RESET)}"
        statuses = ",".join(s or "?" for s in f["statuses"]) or (f["executionStatus"] or "ERROR")
        ref = _tri(f["referenceOk"], "ok", "✗")
        pages = "—" if f["pages"] is None else str(f["pages"])
        seconds = f"{f['durationSeconds']:.0f}" if f["durationSeconds"] else "—"
        out(f"   {mark} {f['file'][:39]:<40}{statuses[:17]:<18}{(f['businessStatus'] or '—')[:18]:<19}"
            f"{_fmt_num(f['confidence']):>6}{f['invalidFields']:>5}{f['failedRules']:>5}{ref:>5}"
            f"{pages:>6}{seconds:>6}{_fmt_money(f['costUsd']):>9}")
        if f["error"]:
            out(f"       {c(RED)}{f['error']}{c(RESET)}")
        if f["groundTruth"] is False:
            out(f"       {c(RED)}ground truth no cumplido{c(RESET)}")
        for signal in f["degradations"]:
            out(f"       {c(YELLOW)}! {signal}{c(RESET)}")

    comparison = a.get("comparison")
    if comparison:
        out(f"\n{c(BOLD)}10. Contra la corrida anterior{c(RESET)} {c(DIM)}({comparison['previousGeneratedAt']}){c(RESET)}")
        out(f"   Tasa de aprobación: {_fmt_pct(comparison['approvalRateBefore'])} → {_fmt_pct(comparison['approvalRateAfter'])}")
        if not (comparison["statusChanges"] or comparison["valueChanges"] or comparison["indexChanges"]):
            out(f"   {c(GREEN)}✓ mismos estados, mismos valores e índices estables{c(RESET)}")
        for ch in comparison["statusChanges"]:
            out(f"   {c(YELLOW)}! {ch['file']}: {ch['before']} → {ch['after']} (negocio {ch['businessBefore']} → {ch['businessAfter']}){c(RESET)}")
        for ch in comparison["valueChanges"][:20]:
            out(f"   {c(YELLOW)}≠ {ch['file']} · {ch['field']}: {ch['before']!r} → {ch['after']!r}{c(RESET)}")
        if len(comparison["valueChanges"]) > 20:
            out(f"   {c(DIM)}… {len(comparison['valueChanges']) - 20} cambio(s) de valor más en analisis.md{c(RESET)}")
        for ch in comparison["indexChanges"]:
            out(f"   {c(YELLOW)}Δ índice {ch['path']}: {ch['before']:.1f} → {ch['after']:.1f}{c(RESET)}")

    out(f"{c(CYAN)}{'═' * 92}{c(RESET)}")
    return "\n".join(lines)


def _md_escape(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(a: dict) -> str:
    ex, docs, cat, v = a["execution"], a["documents"], a["catalog"], a["verdict"]
    md: list[str] = []
    out = md.append
    out(f"# Análisis E2E del dataset — {a['docType']} ({a.get('environment') or '-'})\n")
    out(f"Generado: {a['generatedAt']} · Carpeta: `{a.get('docsDir')}` · Archivos: {ex['files']}\n")
    out(f"## Veredicto: **{v['level'].replace('_', ' ')}**\n")
    for text in v["blockers"]:
        out(f"- ❌ {_md_escape(text)}")
    for text in v["reservations"]:
        out(f"- ⚠️ {_md_escape(text)}")
    for text in v["notes"]:
        out(f"- ℹ️ {_md_escape(text)}")
    out("")
    out("> Cómo leerlo: **NO CONFIABLE** bloquea (falla de infraestructura, ground truth incumplido o un documento "
        "APROBADO con número de pedimento incoherente — el peor caso, porque llega al ERP sin revisión). "
        "**CON RESERVAS** no falla la corrida pero pide atención. La confianza del modelo es auto-reportada: "
        "el índice por parámetro sirve para encontrar los campos débiles, no como prueba de exactitud.\n")

    out("## 1. Ejecución\n")
    out("| Métrica | Valor |\n|---|---|")
    out(f"| Infraestructura OK | {ex['infraPassed']}/{ex['files']} |")
    out(f"| Procesamiento SUCCEEDED | {ex['stepFunctionsSucceeded']}/{ex['files']} |")
    out(f"| Ground truth | {ex['groundTruthPassed']}/{ex['groundTruthEvaluated']} evaluados |")
    for label, key in (("Tiempo E2E (s)", "e2eSeconds"), ("Tiempo pipeline (s)", "pipelineSeconds")):
        d = ex[key]
        if d.get("n"):
            out(f"| {label} | p50 {d['p50']:.0f} · p95 {d['p95']:.0f} · máx {d['max']:.0f} |")
    if ex["costUsd"].get("n"):
        out(f"| Costo | {_fmt_money(ex['costUsd']['sum'])} total · {_fmt_money(ex['costUsd']['mean'])}/archivo · "
            f"{_fmt_money(ex['costPerPageUsd'])}/página |")
    estimate_lines = _cost_estimate_lines(ex.get("costEstimate"))
    if estimate_lines:
        out(f"| Costo estimado antes de correr | {' · '.join(line.strip() for line in estimate_lines)} |")
    out(f"| Páginas | {ex['pages']} (OCR {ex['ocrPages']}) |")
    if ex.get("pageSelection"):
        out(f"| Selección de páginas | {ex['pageSelection']['pagesExtracted']}/{ex['pageSelection']['pagesConsidered']} extraídas |")
    out(f"| Catálogo | local {cat['localSchemaVersion']} · desplegado {', '.join(cat['deployedTypeVersions']) or '—'}"
        f"{' ⚠️ drift' if cat['drift'] else ''} · min_confidence {cat['minConfidence']} |")
    out("")

    out("## 2. Resultado de negocio\n")
    out("| Estado | Documentos | % |\n|---|---:|---:|")
    for status, n in docs["byStatus"].items():
        out(f"| {status} | {n} | {_fmt_pct(n / docs['total'] if docs['total'] else None)} |")
    out(f"\nTasa de aprobación: **{_fmt_pct(docs['approvalRate'])}** · Validación pública: "
        + (" · ".join(f"{k} {n}" for k, n in docs["businessStatus"].items()) or "—"))
    if docs["confidence"].get("n"):
        d = docs["confidence"]
        out(f"\nConfianza por documento: media {d['mean']:.3f} · p50 {d['p50']:.3f} · mín {d['min']:.3f} · σ {d['stdev']:.3f}")
    if docs["otherSegments"]:
        out(f"\nOtros segmentos: " + ", ".join(f"{k} {n}" for k, n in docs["otherSegments"].items()))
    out("")

    out("## 3. Confianza por parámetro\n")
    out(f"Índice = confianza media donde el campo aplica (presente, o inválido). Un obligatorio faltante cuenta 0 y un "
        f"inválido se topa en {cat['minConfidence']}. Ordenado del más bajo al más alto. Los campos de filas de arreglo "
        "(`FACTURAS[].Factura`) cuentan una ocurrencia por fila.\n")
    out("| Campo | Req | Ocurr. | Llenado | Válido | Conf. media | Conf. mín | p10 | σ | < mín | Índice | Origen | Motivos de invalidez |")
    out("|---|:-:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
    for r in a["fields"]:
        conf = r["confidence"]
        reasons = "; ".join(f"{t} ×{n}" for t, n in r["topReasons"])
        index = _fmt_num(r["index"], 1)
        out(f"| `{r['path']}` | {'sí' if r['required'] else ''} | {r['occurrences']} | {_fmt_pct(r['fillRate'])} | "
            f"{_fmt_pct(r['validRate'])} | {_fmt_num(conf.get('mean'), 3)} | {_fmt_num(conf.get('min'), 3)} | "
            f"{_fmt_num(conf.get('p10'), 3)} | {_fmt_num(conf.get('stdev'), 3)} | {r['lowConfidence']} | "
            f"**{index}** | {_md_escape(_short_sources(r['sources']))} | {_md_escape(reasons)} |")
    if a["neverExtracted"]:
        out(f"\n**Nunca extraídos** (opcionales vacíos en todo el dataset): {', '.join(f'`{p}`' for p in a['neverExtracted'])}")
    if a["flatConfidenceFields"]:
        out(f"\n**Confianza plana** (idéntica en todas las lecturas, no discrimina): {', '.join(f'`{p}`' for p in a['flatConfidenceFields'])}")
    out("")

    if a["arrays"]:
        out("## 4. Arreglos\n")
        out("| Arreglo | Docs | Filas mín | Filas media | Filas máx | Vacíos |\n|---|---:|---:|---:|---:|---:|")
        for r in a["arrays"]:
            out(f"| `{r['path']}` | {r['documents']} | {r['rowsMin']:.0f} | {r['rowsMean']:.1f} | {r['rowsMax']:.0f} | {r['emptyDocuments']} |")
        out("")

    out("## 5. Reglas del motor\n")
    if a["failingRules"]:
        out("| Regla | Campo | Fallas | Evaluadas | Severidad | Archivos | Mensaje |\n|---|---|---:|---:|---|---|---|")
        for r in a["failingRules"]:
            out(f"| {r['rule']} | `{r['field']}` | {r['failed']} | {r['evaluated']} | {r['severity']} | "
                f"{_md_escape(', '.join(r['files']))} | {_md_escape(r['topMessage'] or '')} |")
    else:
        out("Ninguna regla falló.")
    if a["inertRules"]:
        out("\n**Reglas inertes** (declaradas en el catálogo, `skipped` en cada documento):\n")
        for r in a["inertRules"]:
            out(f"- `{r['rule']}` en `{r['field']}` — {_md_escape(r['topMessage'] or '')}")
    out("")

    out("## 6. Coherencia interna\n")
    coh = a["coherence"]
    out("| Chequeo | Pasan |\n|---|---:|")
    out(f"| Número = año+aduana+patente+consecutivo | {coh['numeroCompuesto']['passed']}/{coh['numeroCompuesto']['evaluated']} |")
    out(f"| Referencia del nombre de archivo vs extraída | {coh['referenciaArchivo']['passed']}/{coh['referenciaArchivo']['evaluated']} |")
    out(f"| Cuadres aritméticos | {coh['aritmetica']['passed']}/{coh['aritmetica']['evaluated']} |")
    for failure in coh["failures"]:
        out(f"\n- ❌ **{_md_escape(failure['file'])}** [{failure['status']}] {_md_escape(failure['check'])}: {_md_escape(failure['detail'])}")
    out("")

    categorical = [r for r in a["fields"] if r["valueDistribution"]]
    if categorical:
        out("## 7. Distribución de valores\n")
        for r in categorical:
            out(f"- `{r['path']}`: " + " · ".join(f"{_md_escape(_clip(v, 80))} ×{n}" for v, n in r["valueDistribution"]))
        out("")

    out("## 8. Peores documentos\n")
    out("| Archivo | Págs | Estado | Confianza | Inválidos | < mín | Reglas fallidas | Índice |\n|---|---|---|---:|---:|---:|---:|---:|")
    for r in a["worstDocuments"]:
        out(f"| {_md_escape(r['file'])} | {r['pages']} | {r['status']} | {_fmt_num(r['confidence'], 3)} | {r['invalidFields']} | "
            f"{r['lowConfidenceFields']} | {r['failedRules']} | {'—' if r['index'] is None else r['index']} |")
    out("")

    out("## 9. Por archivo\n")
    out("| ✓ | Archivo | documentId | Estados | Negocio | Ground truth | Referencia | Cuadre | Págs (OCR) | E2E s | Costo real | Estimado (tope) | Señales |")
    out("|:-:|---|---|---|---|---|---|---|---|---:|---:|---:|---|")
    for f in a["perFile"]:
        signals = "; ".join(f["degradations"] + ([f["error"]] if f["error"] else [])
                            + ([f["groundTruthNote"]] if f.get("groundTruthNote") else []))
        statuses = ", ".join(s or "?" for s in f["statuses"]) or (f["executionStatus"] or "—")
        pages = f"{'—' if f['pages'] is None else f['pages']} ({'—' if f['ocrPages'] is None else f['ocrPages']})"
        seconds = f"{f['durationSeconds']:.0f}" if f["durationSeconds"] else "—"
        est = f.get("costEstimate") or {}
        estimate = (f"{_fmt_money(est['totalUsd'])} ({_fmt_money(est.get('totalHighUsd'))})"
                    if est.get("totalUsd") is not None else "—")
        out(f"| {'✓' if f['passed'] else '✗'} | {_md_escape(f['file'])} | `{f['documentId'] or '—'}` | {statuses} | "
            f"{f['businessStatus'] or '—'} | {_tri(f['groundTruth'])} | {_tri(f['referenceOk'])} | {_tri(f['arithmeticOk'])} | "
            f"{pages} | {seconds} | {_fmt_money(f['costUsd'])} | {estimate} | {_md_escape(signals)} |")
    out("")

    comparison = a.get("comparison")
    if comparison:
        out(f"## 10. Contra la corrida anterior ({comparison['previousGeneratedAt']})\n")
        out(f"Tasa de aprobación: {_fmt_pct(comparison['approvalRateBefore'])} → {_fmt_pct(comparison['approvalRateAfter'])}\n")
        for ch in comparison["statusChanges"]:
            out(f"- Estado **{_md_escape(ch['file'])}**: {ch['before']} → {ch['after']} (negocio {ch['businessBefore']} → {ch['businessAfter']})")
        for ch in comparison["valueChanges"]:
            out(f"- Valor **{_md_escape(ch['file'])}** · `{ch['field']}`: `{_md_escape(ch['before'])}` → `{_md_escape(ch['after'])}`")
        for ch in comparison["indexChanges"]:
            out(f"- Índice `{ch['path']}`: {ch['before']:.1f} → {ch['after']:.1f}")
        if not (comparison["statusChanges"] or comparison["valueChanges"] or comparison["indexChanges"]):
            out("Sin cambios de estado, valores ni índices.")
    return "\n".join(md) + "\n"
