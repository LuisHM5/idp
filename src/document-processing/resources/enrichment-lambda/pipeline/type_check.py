"""Verificación de tipo: ¿el archivo ES el documento que se declaró?

En la ruta con tipo declarado el segmentador no corre (un solo segmento con
confianza 1.0, sin llamada al modelo) y el extractor recibe el texto con la
orden "Documento declarado como X: llena el schema". Un archivo ajeno — una
constancia fiscal subida a la casilla del estado de cuenta — salía mal solo
por accidente: si el modelo no encontraba nada fallaban los `presence` y el
documento se rechazaba con confianza 0; pero si compartía dos o tres campos
(un RFC, una fecha, una razón social) quedaba PARCIAL con confianza 0.6-0.8 y
nadie se enteraba de que era otro documento.

Este módulo resuelve eso SIN una llamada extra a Bedrock: el extractor pide,
en el mismo tool call de extracción, una llave `_DOCUMENTO` (`corresponde`,
`confianza`, `descripcion`; ver `tool_property`/`PROMPT_RULE`) y aquí se
decide qué hacer con la respuesta:

- `merge_signals` combina la respuesta de varios chunks en fail-open: si
  ALGÚN chunk reconoce el documento, es el documento (un anexo o una página
  de términos no tumba un pedimento de 16 páginas).
- `evaluate` convierte la señal en un `TypeCheck`, con una GUARDIA gratis
  para que el modelo no pueda tumbar solo un documento que claramente se
  leyó: si ≥ `min_required_filled` de los campos `required` salieron llenos
  con confianza suficiente, la señal se degrada a revisión. El default es
  1.0 — TODOS los obligatorios — y no "la mayoría": medido el 2026-10-08
  con MiniMax sobre la constancia fiscal subida como estado de cuenta, el
  modelo llenó 2 de 3 (`TITULAR` con la razón social del contribuyente,
  `PAIS` = MEX) y con 0.5 el archivo ajeno se escapaba a revisión; un
  documento legítimo bien leído tiene todos sus obligatorios, y si le falta
  uno iba a revisión de todos modos. Si el tipo declara
  `page_selection.markers`, los marcadores deciden en los dos sentidos
  (presentes → degrada; ausentes → rechazo pleno aunque haya campos llenos:
  son evidencia más fuerte que tres campos genéricos).
- `mismatch_check` es la verificación que entra a `documents[i].checks`
  por el mismo camino que cualquier regla (`on_fail` → `blocking`), y
  `cap_confidence` baja `documents[i].confidence` casi a 0.
- `aggregate` resume los documentos principales de un archivo para la raíz
  del resultado (de ahí salen `documentMatchesType`/`rejectionConfidence`/
  `detectedDocument` del envelope).

Detección por FORMA del catálogo, nunca por TYPE_ID: aplica a todo tipo con
`fields`; un tipo lo ajusta con el bloque opcional `type_check` (`enabled`,
`on_fail`, `min_required_filled`). Kill switch global fail-open:
`TYPE_CHECK_ENABLED=false` deja `evaluated=False` y no toca estatus ni
confianza — igual que `QR_RECOVERY_ENABLED`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from validators.engine import RuleCheck, _is_empty

from .page_selector import _DEFAULT_MIN_SCORE, score_pages

logger = logging.getLogger(__name__)

# Llave top-level del tool schema de extracción que lleva la señal. El guion
# bajo inicial la aparta de cualquier campo del catálogo (UPPER_SNAKE_CASE sin
# prefijo) y `_parse_field_tree_response` la ignora porque no está en el árbol.
SIGNAL_KEY = "_DOCUMENTO"
# Nombre con el que la verificación aparece en `documents[i].checks`.
RULE_NAME = "document_type_match"
# Techo de `documents[i].confidence` cuando el archivo no corresponde al tipo:
# "casi 0", nunca 0 exacto, para distinguirlo de un documento sin campos.
TYPE_MISMATCH_CONFIDENCE_CAP = 0.05

REASON_MISMATCH = "DOCUMENTO_NO_CORRESPONDE"
REASON_CONTRADICTED = "SEÑAL_CONTRADICHA_POR_CAMPOS"

DEFAULT_ON_FAIL = "reject"
DEFAULT_MIN_REQUIRED_FILLED = 1.0
_ON_FAIL_VALUES = ("reject", "review", "info")

PROMPT_RULE = (
    "- Antes de extraer, decide si el texto ES ese documento y reportalo en la llave "
    f"\"{SIGNAL_KEY}\": \"corresponde\" = false SOLO si es un documento de otra naturaleza "
    "(p. ej. una factura cuando se esperaba una identificacion, una constancia fiscal cuando se "
    "esperaba un estado de cuenta). Una variante extranjera, de otro banco, pais o formato, "
    "una version vieja o un escaneo de mala calidad del MISMO documento SI corresponde. "
    "\"confianza\" 0.0-1.0 de esa decision; \"descripcion\" = que es en realidad, maximo 15 "
    "palabras. Extrae los campos de todos modos.\n"
)


def enabled() -> bool:
    return os.environ.get("TYPE_CHECK_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def tool_property() -> Dict[str, Any]:
    """Schema JSON de la llave `_DOCUMENTO` que se suma al tool de extracción."""
    return {
        "type": "object",
        "description": "Si el texto realmente es el documento declarado.",
        "properties": {
            "corresponde": {"type": "boolean"},
            "confianza": {"type": "number"},
            "descripcion": {"type": "string"},
        },
        "required": ["corresponde", "confianza"],
    }


# ------------------------------------------------------------------ señal


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "si", "sí", "yes", "1"):
            return True
        if v in ("false", "no", "0"):
            return False
    return None


def _as_confidence(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def read_signal(parsed: Any) -> Optional[Dict[str, Any]]:
    """La señal de UNA respuesta del modelo, normalizada, o None si no vino.

    Tolera las envolturas que ya se le han visto a MiniMax en los objetos
    (`{"value": {...}, "confidence": ...}`) y booleanos como texto. Sin
    `corresponde` legible no hay señal: nunca se adivina.
    """
    if not isinstance(parsed, dict):
        return None
    raw = parsed.get(SIGNAL_KEY)
    if isinstance(raw, dict) and isinstance(raw.get("value"), dict):
        raw = raw["value"]
    if not isinstance(raw, dict):
        return None
    matches = _as_bool(raw.get("corresponde"))
    if matches is None:
        return None
    description = raw.get("descripcion")
    description = str(description).strip() if isinstance(description, (str, int, float)) else ""
    return {
        # Sin número el modelo igual decidió; 0.5 = "no dijo qué tan seguro".
        "corresponde": matches,
        "confianza": _as_confidence(raw.get("confianza"), 0.5),
        "descripcion": description[:200] or None,
    }


def merge_signals(signals: Iterable[Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Una señal para el documento a partir de las de sus chunks, fail-open.

    - Algún chunk dice que corresponde → corresponde (la confianza es la
      máxima de los que lo reconocieron).
    - Todos dicen que no → no corresponde con la confianza MÍNIMA de ellos
      (el rechazo vale lo que su chunk menos seguro) y la primera descripción.
    - Ninguna señal → None (no evaluado).
    """
    present = [s for s in signals if s]
    if not present:
        return None
    positives = [s for s in present if s["corresponde"]]
    if positives:
        return {
            "corresponde": True,
            "confianza": max(s["confianza"] for s in positives),
            "descripcion": None,
            "signals": len(present),
        }
    return {
        "corresponde": False,
        "confianza": min(s["confianza"] for s in present),
        "descripcion": next((s["descripcion"] for s in present if s["descripcion"]), None),
        "signals": len(present),
    }


# --------------------------------------------------------------- política


def policy_for(type_entry: Any) -> Dict[str, Any]:
    """El bloque `type_check` del tipo sobre los defaults. Un valor que no se
    entiende cae al default en vez de apagar o endurecer la verificación."""
    raw = getattr(type_entry, "type_check", None)
    raw = raw if isinstance(raw, dict) else {}
    on_fail = str(raw.get("on_fail") or DEFAULT_ON_FAIL).strip().lower()
    if on_fail not in _ON_FAIL_VALUES:
        on_fail = DEFAULT_ON_FAIL
    try:
        min_filled = float(raw.get("min_required_filled", DEFAULT_MIN_REQUIRED_FILLED))
    except (TypeError, ValueError):
        min_filled = DEFAULT_MIN_REQUIRED_FILLED
    enabled_flag = raw.get("enabled", True)
    return {
        "enabled": enabled_flag is not False,
        "on_fail": on_fail,
        "min_required_filled": max(0.0, min(1.0, min_filled)),
    }


def applies_to(type_entry: Any) -> bool:
    """Un tipo field-centric con la verificación encendida (catálogo + kill switch)."""
    return bool(getattr(type_entry, "field_tree", None)) and enabled() and policy_for(type_entry)["enabled"]


# ------------------------------------------------------------- evaluación


@dataclass
class TypeCheck:
    evaluated: bool
    matches: Optional[bool]            # None cuando no se evaluó
    rejection_confidence: float        # 0.0 cuando corresponde o no se evaluó
    detected: Optional[str]            # qué es en realidad, según el modelo
    reason: Optional[str]              # REASON_MISMATCH | REASON_CONTRADICTED | None
    guard: Dict[str, Any]

    def to_json(self) -> Dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "matches": self.matches,
            "rejectionConfidence": self.rejection_confidence,
            "detected": self.detected,
            "reason": self.reason,
            "guard": self.guard,
        }


def not_evaluated(why: str) -> TypeCheck:
    return TypeCheck(False, None, 0.0, None, None, {"skipped": why})


def _required_scalars(field_tree: Dict[str, Any]) -> List[str]:
    """Campos `required` de primer nivel que el modelo lee (no `computed`);
    los objetos cuentan como uno — `_is_empty` ya sabe mirarles adentro."""
    return [
        name for name, spec in (field_tree or {}).items()
        if getattr(spec, "required", False) and not getattr(spec, "computed", False)
    ]


def _required_filled(entity_map: Dict[str, Any], confidences: Dict[str, float],
                     type_entry: Any) -> Dict[str, int]:
    names = _required_scalars(getattr(type_entry, "field_tree", None) or {})
    threshold = float(getattr(type_entry, "min_confidence", 0.70) or 0.0)
    filled = 0
    for name in names:
        value = (entity_map or {}).get(name)
        if _is_empty(value):
            continue
        conf = (confidences or {}).get(name)
        if conf is None or float(conf) >= threshold:
            filled += 1
    return {"requiredFilled": filled, "requiredTotal": len(names)}


def _markers_verdict(type_entry: Any, pages: Optional[Sequence[Any]]) -> Optional[str]:
    """`MATCHED`/`ABSENT` según los marcadores de `page_selection`, None si el
    tipo no los declara o no hay páginas con texto que mirar."""
    spec = getattr(type_entry, "page_selection", None)
    markers = list((spec or {}).get("markers") or []) if isinstance(spec, dict) else []
    if not markers or not pages:
        return None
    if not any((getattr(p, "text", "") or "").strip() for p in pages):
        return None
    min_score = int((spec or {}).get("min_score", _DEFAULT_MIN_SCORE))
    scores = score_pages(pages, markers)
    return "MATCHED" if any(s.score >= min_score for s in scores) else "ABSENT"


def evaluate(
    signal: Optional[Dict[str, Any]],
    entity_map: Dict[str, Any],
    confidences: Dict[str, float],
    type_entry: Any,
    pages: Optional[Sequence[Any]] = None,
) -> TypeCheck:
    """La decisión para un documento ya extraído (ver docstring del módulo)."""
    if not enabled():
        return not_evaluated("TYPE_CHECK_ENABLED=false")
    policy = policy_for(type_entry)
    if not policy["enabled"]:
        return not_evaluated("type_check.enabled=false")
    if not signal:
        return not_evaluated("sin señal del modelo")

    confidence = round(float(signal.get("confianza") or 0.0), 4)
    if signal.get("corresponde"):
        return TypeCheck(True, True, 0.0, None, None, {"modelConfidence": confidence})

    guard: Dict[str, Any] = {"modelConfidence": confidence}
    guard.update(_required_filled(entity_map, confidences, type_entry))
    markers = _markers_verdict(type_entry, pages)
    if markers is not None:
        guard["markers"] = markers
        contradicted = markers == "MATCHED"
    else:
        total = guard["requiredTotal"]
        ratio = (guard["requiredFilled"] / total) if total else 0.0
        guard["requiredRatio"] = round(ratio, 4)
        contradicted = total > 0 and ratio >= policy["min_required_filled"]
    reason = REASON_CONTRADICTED if contradicted else REASON_MISMATCH
    return TypeCheck(True, False, confidence, signal.get("descripcion"), reason, guard)


# ----------------------------------------------------------------- efecto


def mismatch_check(type_check: Optional[Dict[str, Any]], type_entry: Any) -> Optional[RuleCheck]:
    """La verificación que entra a `documents[i].checks` cuando el archivo no
    corresponde; None cuando corresponde o no se evaluó.

    Grado por `type_check.on_fail` del tipo (default `reject` → `blocking`,
    el documento se RECHAZA por esto solo). Con la señal contradicha por los
    campos leídos el efecto baja un escalón: `reject` → `review`; `review` e
    `info` se quedan como están.
    """
    if not type_check or type_check.get("matches") is not False:
        return None
    on_fail = policy_for(type_entry)["on_fail"]
    contradicted = type_check.get("reason") == REASON_CONTRADICTED
    if contradicted and on_fail == "reject":
        on_fail = "review"
    label = getattr(type_entry, "display_name", "") or getattr(type_entry, "type_id", "el tipo declarado")
    detected = type_check.get("detected")
    message = f"El archivo no es un {label}"
    message += f": parece {detected}" if detected else " según su contenido"
    if contradicted:
        message += " (la lectura de los campos obligatorios contradice esa señal; revisar)"
    check = RuleCheck(
        field=SIGNAL_KEY,
        rule=RULE_NAME,
        valid=False,
        message=message,
        severity={"reject": "error", "review": "warning", "info": "info"}[on_fail],
        blocking=on_fail == "reject",
        details={
            "reason": type_check.get("reason"),
            "rejectionConfidence": type_check.get("rejectionConfidence"),
            "detected": detected,
            "onFail": on_fail,
        },
    )
    return check


def cap_confidence(type_check: Optional[Dict[str, Any]], confidence: float) -> float:
    """`documents[i].confidence` casi a 0 cuando el archivo no corresponde.
    Los campos conservan su valor y su propia confianza: lo que cae es el
    número del documento."""
    if type_check and type_check.get("matches") is False:
        return min(float(confidence or 0.0), TYPE_MISMATCH_CONFIDENCE_CAP)
    return confidence


def aggregate(documents: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Resumen para la raíz del resultado sobre los documentos principales.

    `matches` es `False` si ALGÚN documento principal no corresponde, `True`
    si todos los evaluados corresponden y `None` si ninguno se evaluó;
    `rejectionConfidence` es la máxima entre los que no corresponden y
    `detected` la del primero de ellos.
    """
    checks = [
        d.get("typeCheck") for d in documents
        if isinstance(d.get("typeCheck"), dict)
        and d.get("documentType") != "OTRO" and d.get("pageStart") is not None
    ]
    evaluated = [c for c in checks if c.get("evaluated") and c.get("matches") is not None]
    if not evaluated:
        return {"matches": None, "rejectionConfidence": 0.0, "detected": None}
    mismatched = [c for c in evaluated if c.get("matches") is False]
    if not mismatched:
        return {"matches": True, "rejectionConfidence": 0.0, "detected": None}
    return {
        "matches": False,
        "rejectionConfidence": max(float(c.get("rejectionConfidence") or 0.0) for c in mismatched),
        "detected": next((c.get("detected") for c in mismatched if c.get("detected")), None),
    }
