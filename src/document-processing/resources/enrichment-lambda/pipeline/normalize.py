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


def digits_exact(value: Any, length: int) -> Optional[str]:
    """Digits of `value`, but only when there are exactly `length` of them.

    Used to validate an atomic pedimento component (ANIO_PEDIMENTO,
    ADUANA_DESPACHO, PATENTE, NUMERO_CONSECUTIVO) before trusting it as an
    input to a deterministic concatenation — a value with the wrong digit
    count is a sign the extraction itself is off, so callers should not use
    it rather than silently pad/truncate.
    """
    digits = normalize_pedimento(value)
    return digits if digits and len(digits) == length else None


def normalize_year_2digit(value: Any) -> Optional[str]:
    """Collapse a 4-digit year to its trailing 2 digits ("2026" -> "26").

    Pedimento numbers encode the year as 2 digits; models sometimes
    "helpfully" expand it back to 4 despite an explicit instruction to
    return only 2 (a real, observed failure — not hypothetical). Collapsing
    the last 2 digits of an unambiguous 19xx/20xx value loses no
    information (they're the same digits either way) and is not invented,
    so it's a safe deterministic fix rather than a guess. Returns the
    2-digit form unchanged if it's already 2 digits; None if it's neither.
    """
    digits = normalize_pedimento(value)
    if digits is None:
        return None
    if len(digits) == 2:
        return digits
    if len(digits) == 4 and digits[:2] in ("19", "20"):
        return digits[2:]
    return None


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


def find_pedimento_components(text: str) -> Optional["tuple[str, str, str, str]"]:
    """Deterministically split a full "AA ADUANA PATENTE CONSECUTIVO"
    pedimento reference into its 4 parts (year, aduana, patente,
    consecutivo), when the full 15-digit form appears in `text`.

    Used to recover an atomic field (e.g. ADUANA_DESPACHO) the model left
    empty even though the full reference — which contains that same digit
    group — was extracted elsewhere on the document. Returns None when the
    full form isn't present (the caller falls back to whatever the model
    extracted directly for that field).
    """
    if not text:
        return None
    m = _PED_FULL_RE.search(text)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


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


_PEDIMENTO_NUMBER_TOKEN_RE = re.compile(r"(?:^|_)(NUMERO|NUM|NO|REFERENCE|NORMALIZADO)(?:_|$)")


def looks_like_pedimento_field(field_name: str) -> bool:
    """True for a field name that reads as "the pedimento number" under any
    naming convention: NUMERO_PEDIMENTO, PEDIMENTO_REFERENCE,
    NUMERO_PEDIMENTO_NORMALIZADO — or an ERP column such as No_Pedimento,
    which the previous substring test ("NUMERO"/"REFERENCE"/"NORMALIZADO" in
    the name) missed, leaving that field un-normalized (spaces kept).
    """
    name = str(field_name).upper()
    return "PEDIMENTO" in name and bool(_PEDIMENTO_NUMBER_TOKEN_RE.search(name))


def normalize_field(field_name: str, value: Any, declared_type: Optional[str] = None) -> Any:
    """Best-effort normalization keyed on the field's DECLARED type first, and
    on field-name conventions only when there is no declared type.

    Amount-like names -> float, date-like names -> ISO date, pedimento
    references -> digit string, RFC/UUID -> canonical casing. Anything else
    returns a stripped string (or the value untouched for lists/dicts).

    `declared_type` is the catalog `type` of the field (field-centric DSL).
    The name heuristics alone cannot tell a number from a string that merely
    MENTIONS one: `num_acuse_valor` contains "VALOR", so the acuse
    "COVE2689JP013 / 35079805" was parsed as an amount and handed on as
    268901335079805.0 -- every letter and the diagonal gone, tagged
    NORMALIZED and valid. The same trap catches ENTIDAD_FEDERAT-IVA-,
    TIPO_SEGURO and VALOR_LITERAL. It cuts the other way too:
    `FACTOR_MONEDA_FACTURA` matches no amount token, so a `number` column came
    back as the string "1.0". The declared type settles both cases; where
    there is none (legacy DSL, or a scalar inside an array, which declares no
    per-item type) the heuristics behave exactly as before.
    """
    if value is None or isinstance(value, (list, dict)):
        return value
    if declared_type == "boolean":
        # Never stringify a boolean into "True", and never invent a parser for
        # whatever else landed here: leave the value for the rules to judge.
        return value
    name = field_name.upper()

    if looks_like_pedimento_field(name):
        return normalize_pedimento(value) or str(value).strip()
    if name.endswith("RFC") or name.startswith("RFC") or "_RFC" in name or name.endswith("TAX_ID"):
        return normalize_rfc(value) or str(value).strip().upper()
    if name == "UUID" or name.endswith("_UUID"):
        return normalize_uuid(value) or str(value).strip().upper()
    if _wants_date(name, declared_type):
        parsed = parse_date_iso(value)
        return parsed if parsed is not None else str(value).strip()
    if _wants_amount(name, declared_type):
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


# Declared types that a name heuristic must never turn into a number. "date"
# is here too: a date field goes through parse_date_iso, never parse_amount.
_NON_NUMERIC_TYPES = frozenset({"string", "date", "boolean"})


def _wants_amount(name: str, declared_type: Optional[str]) -> bool:
    """Should this value be parsed into a float? The catalog decides when it
    declared a type; the name heuristics only fill in for what didn't."""
    if declared_type == "number":
        return True
    if declared_type in _NON_NUMERIC_TYPES:
        return False
    return _looks_like_amount_field(name)


def _wants_date(name: str, declared_type: Optional[str]) -> bool:
    if declared_type == "date":
        return True
    if declared_type == "number":
        return False
    return _looks_like_date_field(name)
