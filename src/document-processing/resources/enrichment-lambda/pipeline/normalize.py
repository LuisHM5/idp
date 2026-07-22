"""Deterministic normalizers for extracted values.

The extraction LLM returns values as they appear on the document; all
normalization (amounts, dates, pedimento numbers, RFC/UUID casing) happens
here so results are reproducible and never hallucinated. Normalized values
are stored alongside the original extracted value, never replacing it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

# ---------------------------------------------------------------- amounts

_AMOUNT_CLEAN_RE = re.compile(r"[^\d.,\-]")


def parse_amount(value: Any) -> Optional[float]:
    """Parse a monetary/numeric string into a float.

    Handles "$ 142,717.00", "1,234.56", "10,800.00", "927.0000", "-53.00",
    European "1.234,56" (only when unambiguous), plain ints/floats.
    Returns None when the value cannot be interpreted as a number.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _AMOUNT_CLEAN_RE.sub("", str(value)).strip()
    if not text or text in {"-", ".", ","}:
        return None

    has_comma, has_dot = "," in text, "." in text
    if has_comma and has_dot:
        # Decimal separator = the rightmost of the two
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        # "1,234" thousands vs "3,50" decimal: treat as decimal only when
        # exactly 1-2 digits follow the last comma
        tail = len(text) - text.rfind(",") - 1
        if tail in (1, 2) and text.count(",") == 1:
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------- dates

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7,
    "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}

_DATE_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DATE_DMY_RE = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})")
_DATE_ES_RE = re.compile(
    r"(\d{1,2})\s*(?:de\s+)?([a-záéíóú]+)\s*(?:de\s+|del\s+)?(\d{4})", re.IGNORECASE
)


def parse_date_iso(value: Any) -> Optional[str]:
    """Extract a date from common MX formats and return ISO 8601 (YYYY-MM-DD).

    Tolerates trailing time components ("2026-05-21T13:19:00",
    "27/05/2026 12:50:11 p. m.") and Spanish long form
    ("21 de mayo de 2026"). Day-first is assumed for slashed dates.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    m = _DATE_ISO_RE.search(text)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        return _safe_date(y, mo, d)
    m = _DATE_DMY_RE.search(text)
    if m:
        d, mo, y = (int(g) for g in m.groups())
        return _safe_date(y, mo, d)
    m = _DATE_ES_RE.search(text.lower())
    if m:
        day, month_name, year = m.groups()
        month = _MONTHS_ES.get(month_name)
        if month:
            return _safe_date(int(year), month, int(day))
    return None


def _safe_date(year: int, month: int, day: int) -> Optional[str]:
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------- pedimento

_DIGITS_RE = re.compile(r"\d")


def normalize_pedimento(value: Any) -> Optional[str]:
    """Normalize a pedimento number to its 15-digit form when possible.

    "26 07 3435 6041248" -> "260734356041248". Returns the digit string even
    when shorter than 15 (e.g. partial references) so callers can decide.
    """
    if value is None:
        return None
    digits = "".join(_DIGITS_RE.findall(str(value)))
    return digits or None


def pedimento_consecutivo(value: Any) -> Optional[str]:
    """Last 7 digits of a pedimento reference — the operation consecutivo.

    References across an expediente rarely share the full 15-digit format
    ("6041248", "P0823 6001187", "-999-9999-6041248", "26 07 3435 6041248"),
    but the 7-digit consecutivo is stable. Used for same_pedimento matching.
    """
    digits = normalize_pedimento(value)
    if not digits or len(digits) < 7:
        return None
    return digits[-7:]


# ------------------------------------------------- pedimento ref recovery

# "26 07 3435 6041248" / "26-07-3435-6041248" (full 15-digit form)
_PED_FULL_RE = re.compile(r"\b(\d{2})\s*[- ]\s*(\d{2})\s*[- ]\s*(\d{4})\s*[- ]\s*(\d{7})\b")
# "P0823 6001187" (patente + consecutivo, common on CFDI references)
_PED_PATENTE_RE = re.compile(r"\bP\s?(\d{3,4})[\s\-]+(\d{7})\b")
# a bare 7-digit consecutivo on a line that talks about pedimento/reference
_PED_KEYWORD_RE = re.compile(r"pedimento|\bped\.?\s|referencia|\bref\.?\s*:|observaciones", re.IGNORECASE)
_PED_CONSECUTIVO_RE = re.compile(r"(?<![\d\-])(\d{7})(?![\d\-])")


def find_pedimento_reference(text: str) -> Optional[str]:
    """Deterministically recover a pedimento reference from document text.

    Order of preference: full 15-digit form, patente+consecutivo form, then a
    bare 7-digit consecutivo on a line mentioning pedimento/REF/observaciones.
    Returns the matched text verbatim (evidence-preserving) or None.
    """
    if not text:
        return None
    m = _PED_FULL_RE.search(text)
    if m:
        return m.group(0).strip()
    m = _PED_PATENTE_RE.search(text)
    if m:
        return m.group(0).strip()
    for line in text.splitlines():
        if _PED_KEYWORD_RE.search(line):
            c = _PED_CONSECUTIVO_RE.search(line)
            if c:
                return c.group(1)
    return None


# ---------------------------------------------------------------- ids

_RFC_RE = re.compile(r"^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$")
_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def normalize_rfc(value: Any) -> Optional[str]:
    """Uppercase and strip an RFC; returns None when it doesn't look like one."""
    if value is None:
        return None
    text = re.sub(r"[\s.\-]", "", str(value)).upper()
    return text if _RFC_RE.match(text) else None


_UNICODE_DASHES = dict.fromkeys(map(ord, "‐‑‒–—−"), "-")


def normalize_uuid(value: Any) -> Optional[str]:
    """Uppercase a CFDI UUID; returns None when malformed.

    PDFs sometimes render UUID hyphens as Unicode dashes (U+2010...);
    those are folded to ASCII '-' before validation.
    """
    if value is None:
        return None
    text = str(value).strip().upper().translate(_UNICODE_DASHES)
    text = re.sub(r"\s", "", text)
    return text if _UUID_RE.match(text) else None


def normalize_field(field_name: str, value: Any) -> Any:
    """Best-effort normalization keyed on field-name conventions.

    Amount-like names -> float, date-like names -> ISO date, pedimento
    references -> digit string, RFC/UUID -> canonical casing. Anything else
    returns a stripped string (or the value untouched for lists/dicts).
    """
    if value is None or isinstance(value, (list, dict)):
        return value
    name = field_name.upper()

    if "PEDIMENTO" in name and ("NUMERO" in name or "REFERENCE" in name or "NORMALIZADO" in name):
        return normalize_pedimento(value) or str(value).strip()
    if name.endswith("RFC") or name.startswith("RFC") or "_RFC" in name or name.endswith("TAX_ID"):
        return normalize_rfc(value) or str(value).strip().upper()
    if name == "UUID" or name.endswith("_UUID"):
        return normalize_uuid(value) or str(value).strip().upper()
    if _looks_like_date_field(name):
        parsed = parse_date_iso(value)
        return parsed if parsed is not None else str(value).strip()
    if _looks_like_amount_field(name):
        parsed = parse_amount(value)
        return parsed if parsed is not None else str(value).strip()
    return str(value).strip()


_AMOUNT_TOKENS = (
    "TOTAL", "SUBTOTAL", "IMPORTE", "VALOR", "PRECIO", "MONTO", "SALDO",
    "AMOUNT", "IVA", "IEPS", "ISR", "DESCUENTO", "ANTICIPO", "HONORARIOS",
    "FLETE", "SEGURO", "PESO", "FREIGHT", "SURCHARGE", "TASA", "CANTIDAD",
    "TIPO_CAMBIO", "EXCHANGE_RATE", "DECREMENTABLE", "INCREMENTABLE",
    "EMBALAJES", "QUANTITY", "WEIGHT", "BASE", "PAGO_CLIENTE",
)
_AMOUNT_NOT_TOKENS = ("FECHA", "DATE", "NUMERO", "NUMBER", "REFERENCIA", "CON_LETRA", "FORMA", "METODO")


def _looks_like_amount_field(name: str) -> bool:
    if any(t in name for t in _AMOUNT_NOT_TOKENS):
        return False
    return any(t in name for t in _AMOUNT_TOKENS)


def _looks_like_date_field(name: str) -> bool:
    return "FECHA" in name or name.endswith("_DATE") or name.startswith("DATE_") or name in {"PICKUP_DATE", "DELIVERY_DATE", "DUE_DATE"}
