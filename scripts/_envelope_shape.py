"""Lee un envelope de resultado sin que importe su versión.

Espejo en Python de `query-lambda/envelope-shape.mjs`. Existe por la misma
razón: a partir de v2 el envelope es plano (`documents[]` en la raíz, con
`fields` en forma de nodo `{value, confidence, valid}`), pero los resultados
guardados de antes siguen ahí y estos scripts los leen años después.

También sirve para el resultado COMPLETO de S3 (`results/{id}.json`), que tiene
la misma raíz que el envelope v2 — esa es justo la razón de haberlos alineado —
aunque sus documentos traigan el detalle profundo (`fields` con
`extractedValue`/`checks`, o el árbol en `extractedEntities`).
"""

from __future__ import annotations

from typing import Any, Dict, List


def is_v2(envelope: Dict[str, Any]) -> bool:
    return int((envelope or {}).get("envelopeVersion") or 0) >= 2


def documents_of(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Los documentos de un envelope (de cualquier versión) o de un resultado
    completo de S3. Un envelope v1 del flujo estándar no tenía ninguno: su
    documento único era el `extractedEntities` de la raíz, y aquí sale como un
    elemento para que quien consume no tenga que distinguir."""
    if not isinstance(obj, dict):
        return []
    docs = obj.get("documents")
    if isinstance(docs, list) and docs:
        return docs
    legacy = (obj.get("expediente") or {}).get("documents")
    if isinstance(legacy, list) and legacy:
        return legacy
    entities = obj.get("extractedEntities")
    if isinstance(entities, dict) and entities:
        return [{
            "documentType": obj.get("documentClassification"),
            "status": (obj.get("validation") or {}).get("status"),
            "extractedEntities": entities,
        }]
    return []


def field_value(doc: Dict[str, Any], name: str) -> Any:
    """El valor de un campo, venga en la forma que venga.

    Orden: nodo de v2 (`fields[name].value`) → detalle legado de
    `resolve_fields` (`normalizedValue`/`extractedValue`) → árbol del resultado
    de S3 (`extractedEntities[name].value`) → escalar pelón de v1.
    """
    slot = (doc.get("fields") or {}).get(name)
    if isinstance(slot, dict):
        if "value" in slot and slot.get("value") not in (None, "", []):
            return slot["value"]
        for key in ("normalizedValue", "extractedValue"):
            if slot.get(key) not in (None, "", []):
                return slot[key]
    node = (doc.get("extractedEntities") or {}).get(name)
    if isinstance(node, dict):
        return node.get("value")
    return node


def field_confidence(doc: Dict[str, Any], name: str) -> Any:
    for source in (doc.get("fields") or {}, doc.get("extractedEntities") or {}):
        slot = source.get(name)
        if isinstance(slot, dict) and isinstance(slot.get("confidence"), (int, float)):
            return slot["confidence"]
    return None


def array_counts(doc: Dict[str, Any]) -> Dict[str, int]:
    """Cuántos renglones trae cada campo tipo arreglo."""
    out: Dict[str, int] = {}
    for source in (doc.get("fields") or {}, doc.get("extractedEntities") or {}):
        for name, node in source.items():
            if isinstance(node, dict) and isinstance(node.get("items"), list):
                out[name] = len(node["items"])
            elif isinstance(node, dict) and isinstance(node.get("count"), int):
                # Arreglo soltado por presupuesto: conserva su conteo.
                out[name] = node["count"]
            elif isinstance(node, list) and node:
                out[name] = len(node)
    return out or dict(doc.get("arrayCounts") or {})
