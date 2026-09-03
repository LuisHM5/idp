"""Tabular document orchestrator: XLSX/CSV bytes -> extracted, validated result.

Sibling to `pipeline/xml_document.py` for document types whose source is
already-structured tabular data (a spreadsheet report, a CSV export) instead
of a PDF/scan or XML. Extraction is a deterministic grid lookup driven by each
field's `cell` (a fixed Excel-style address, e.g. "B2") or, for repeatable
rows, `column` (matched against a header row) — a field-centric-DSL extra,
see catalogs/README.md. No Bedrock call, no OCR.

Both raw formats parse into the SAME grid shape (`{sheet_name: [[cell, ...],
...]}`), so every field extraction, cell-address, and column-lookup helper
below is 100% shared: a CSV is just a workbook with one implicit sheet. Only
`_parse_xlsx`/`_parse_csv` differ.

Like XML, a type opts into this path simply by declaring `cell`/`column` on
its `fields`; nothing here is coupled to any TYPE_ID. `handler.py` routes by
the uploaded file's extension (`.xlsx`/`.xlsm`/`.csv`), and only a single,
standalone document is supported per upload (no multi-sheet-as-multi-document
"expediente") — always with an explicit `declared_type`, same constraint and
same reasoning as XML (see xml_document.py's module docstring).

COST GOTCHA specific to CSV (not XML/XLSX): Bedrock Converse already treats
`csv` as a natively supported document format (see model-invoke-lambda's
`_DOC_FORMATS`), so an UNDECLARED csv upload would actually reach a real
Bedrock classification call instead of failing fast like an undeclared xlsx/
xml does. `upload-lambda` closes this by requiring `documentType` for xml/
xlsx/csv uploads at the API boundary — see index.mjs.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from document_catalog import FieldSpec, TypeRegistryEntry, get_type_entry
from validators import engine
from validators.engine import ValidationContext

from . import confidence as conf_policy
from .derived import apply_derived_fields
from .liquidacion import apply_liquidacion_normalizada
from .normalize import parse_amount
from .result_common import build_tracking, checks_to_json, error_result

logger = logging.getLogger(__name__)

_TABULAR_CONFIDENCE = 1.0
_SOURCE_LABEL = "deterministic:tabular"

# CSV has no sheet concept; every cell/column field resolves against this
# single implicit sheet when the field doesn't declare its own `sheet`.
_CSV_SHEET_NAME = "Sheet1"

Grid = Dict[str, List[List[Any]]]  # sheet name -> rows -> cell values (raw)


# ------------------------------------------------------------------ parsing

def _parse_xlsx(data: bytes) -> Grid:
    import openpyxl  # deferred: only pulled in for this pipeline

    # data_only=True reads formula cells' last-computed value (what a human
    # sees open in Excel), not the formula string — real reports routinely
    # use formulas for totals. read_only=True streams instead of building the
    # full object model, since nothing here needs to write back.
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    try:
        return {ws.title: [list(row) for row in ws.iter_rows(values_only=True)] for ws in wb.worksheets}
    finally:
        wb.close()


def _parse_csv(data: bytes) -> Grid:
    # utf-8-sig tolerates the BOM Excel prepends when it exports/saves CSV.
    text = data.decode("utf-8-sig")
    return {_CSV_SHEET_NAME: list(csv.reader(io.StringIO(text)))}


def parse_grid(data: bytes, source_format: str) -> Grid:
    if source_format == "csv":
        return _parse_csv(data)
    return _parse_xlsx(data)  # xlsx/xlsm share the same parser


# ------------------------------------------------------------- cell/column

_CELL_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _cell_to_rowcol(ref: str) -> Tuple[int, int]:
    """Excel-style "B2" -> 0-indexed (row, col). Self-contained (no openpyxl
    dependency) so the same address math works uniformly for CSV grids too."""
    m = _CELL_RE.match(ref.strip())
    if not m:
        raise ValueError(f"invalid cell reference: {ref!r}")
    col_letters, row_num = m.group(1).upper(), int(m.group(2))
    col_idx = 0
    for ch in col_letters:
        col_idx = col_idx * 26 + (ord(ch) - ord("A") + 1)
    return row_num - 1, col_idx - 1


def _column_letter_to_index(letters: str) -> int:
    _, col = _cell_to_rowcol(f"{letters}1")
    return col


def _resolve_sheet_names(grid: Grid, sheet_spec: Any) -> List[str]:
    """Resolve a `sheet`/`item_sheet` declaration to concrete sheet names, in
    read order.

    - Omitted (None) -> just the workbook's first sheet, unchanged default so
      every existing single-sheet catalog needs no change.
    - `"*"` -> every sheet in the workbook, in file order.
    - A list -> exactly those sheets, in the given order. A name that isn't
      in the workbook is skipped (logged), not silently substituted — for a
      multi-sheet read, swapping in the wrong sheet would corrupt/duplicate
      the aggregated result instead of just leaving one field/row empty.
    - A single string -> that one sheet, same skip-if-missing behavior.

    Callers decide how to USE the resolved list: an array field concatenates
    rows from every sheet in it; a scalar field tries each in order and takes
    the first cell that isn't empty (so `sheet` can also express "try this
    sheet, else that one" for a value that moves between sheet layouts).
    """
    if sheet_spec is None:
        first = next(iter(grid.keys()), None)
        return [first] if first else []
    if sheet_spec == "*":
        return list(grid.keys())
    names = sheet_spec if isinstance(sheet_spec, list) else [sheet_spec]
    resolved = []
    for name in names:
        if name in grid:
            resolved.append(name)
        else:
            logger.warning("declared sheet '%s' not found in workbook; skipped", name)
    return resolved


def _get_cell(rows: List[List[Any]], ref: str) -> Any:
    row_idx, col_idx = _cell_to_rowcol(ref)
    if 0 <= row_idx < len(rows):
        row = rows[row_idx]
        if 0 <= col_idx < len(row):
            return row[col_idx]
    return None


def _norm_header(value: Any) -> str:
    return str(value if value is not None else "").strip().upper()


def _find_column(header: List[Any], name: str) -> Optional[int]:
    target = _norm_header(name)
    for idx, cell in enumerate(header):
        if _norm_header(cell) == target:
            return idx
    return None


def _is_blank_row(row: List[Any]) -> bool:
    return not any(c not in (None, "") for c in row)


def _data_rows(rows: List[List[Any]], array_params: Dict[str, Any]) -> Tuple[List[Any], List[List[Any]]]:
    """Resolve (header_row, data_rows) for an array field from its own
    params: `header_row` (1-indexed, default 1), `start_row` (default
    header_row + 1), optional `end_row` (inclusive). Without an explicit
    `end_row`, reading stops at the first fully-blank row — the common case
    for a table with nothing else below it on the sheet."""
    header_row_num = int(array_params.get("header_row", 1))
    start_row_num = int(array_params.get("start_row", header_row_num + 1))
    end_row_num = array_params.get("end_row")
    header = rows[header_row_num - 1] if 0 <= header_row_num - 1 < len(rows) else []

    data: List[List[Any]] = []
    idx = start_row_num - 1
    while idx < len(rows):
        if end_row_num is not None and idx >= int(end_row_num):
            break
        row = rows[idx]
        if _is_blank_row(row):
            if end_row_num is None:
                break
            idx += 1
            continue
        data.append(row)
        idx += 1
    return header, data


def _row_value(row: List[Any], header: List[Any], params: Dict[str, Any]) -> Any:
    col_idx: Optional[int] = None
    if params.get("column"):
        col_idx = _find_column(header, params["column"])
    elif params.get("column_letter"):
        col_idx = _column_letter_to_index(params["column_letter"])
    if col_idx is None or not (0 <= col_idx < len(row)):
        return None
    return row[col_idx]


# ------------------------------------------------------------------ coerce

def _coerce(raw: Any, ftype: str) -> Any:
    """Cast a raw grid cell to the field's declared type.

    openpyxl hands back native Python types for formatted cells (int/float/
    datetime/bool), not just strings like an XML attribute or a CSV cell
    always is — this normalizes both sources to the same output contract
    `resolve_field_tree` expects (see `text_extractor._parse_field_tree_response`).
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw if ftype == "boolean" else str(raw)
    if isinstance(raw, (datetime, date)):
        return raw.isoformat()
    if isinstance(raw, (int, float)):
        return float(raw) if ftype == "number" else str(raw).strip()
    text = str(raw).strip()
    if not text:
        return None
    if ftype == "number":
        # CSV/typed-as-text XLSX cells can carry "$1,234.56"-style formatting
        # that a bare float() can't parse — parse_amount already handles the
        # full range of money formats used elsewhere in this pipeline.
        parsed = parse_amount(text)
        return parsed if parsed is not None else text
    if ftype == "boolean":
        return text.lower() in ("true", "1", "si", "sí", "yes", "x")
    return text


# -------------------------------------------------------------- extraction

def _extract_node(
    grid: Grid, tree: Dict[str, FieldSpec], default_sheet: Optional[str], prefix: str = ""
) -> Tuple[Dict[str, Any], Dict[str, float]]:
    entity: Dict[str, Any] = {}
    confidences: Dict[str, float] = {}
    for name, spec in tree.items():
        path = f"{prefix}{name}"
        if spec.computed:
            entity[name] = None
            continue
        if spec.type == "object":
            sub_entity, sub_conf = _extract_node(grid, spec.fields, default_sheet, path + ".")
            entity[name] = sub_entity
            confidences.update(sub_conf)
        elif spec.type == "array":
            # item_sheet: a single sheet (default — every existing catalog
            # keeps working unchanged), a list of sheets (rows concatenated
            # in the given order — e.g. one tab per month, same columns), or
            # "*" (every sheet in the workbook). The SAME header_row/
            # start_row/end_row/item_fields mapping applies to each sheet —
            # this assumes a consistent layout across sheets, the common
            # real-world case for that shape of workbook.
            sheet_spec = spec.params.get("item_sheet") or default_sheet
            sheet_names = _resolve_sheet_names(grid, sheet_spec)
            items: List[Dict[str, Any]] = []
            if spec.item_fields:
                for sheet_name in sheet_names:
                    header, data_rows = _data_rows(grid.get(sheet_name, []), spec.params)
                    for row in data_rows:
                        row_entity: Dict[str, Any] = {}
                        for sub_name, sub_spec in spec.item_fields.items():
                            raw = _row_value(row, header, sub_spec.params)
                            row_entity[sub_name] = _coerce(raw, sub_spec.type) if raw not in (None, "") else None
                        items.append(row_entity)
            entity[name] = items
            if items:
                confidences[path] = _TABULAR_CONFIDENCE
        else:
            cell = spec.params.get("cell")
            if not cell:
                entity[name] = None
                continue
            # `sheet` on a scalar means "try these sheets in order, first
            # non-empty cell wins" (useful when the same field moves between
            # sheet layouts) rather than aggregation — a single value can't
            # meaningfully come from several sheets at once. "*" degrades to
            # "search every sheet for this cell", which is a sensible (if
            # unusual) reading, not an error.
            sheet_spec = spec.params.get("sheet") or default_sheet
            raw = None
            for sheet_name in _resolve_sheet_names(grid, sheet_spec):
                candidate = _get_cell(grid.get(sheet_name, []), cell)
                if candidate not in (None, ""):
                    raw = candidate
                    break
            value = _coerce(raw, spec.type)
            entity[name] = value
            if value not in (None, ""):
                confidences[path] = _TABULAR_CONFIDENCE
    return entity, confidences


def extract_from_grid(grid: Grid, type_entry: TypeRegistryEntry) -> Tuple[Dict[str, Any], Dict[str, float]]:
    """Deterministically extract a catalog's `field_tree` from a parsed
    XLSX/CSV grid. Public so tests/other callers can exercise it directly."""
    default_sheet = next(iter(grid.keys()), None)
    return _extract_node(grid, type_entry.field_tree, default_sheet)


# --------------------------------------------------------------- orchestrate

def process_tabular(
    data: bytes,
    source_format: str,
    declared_type: Optional[str] = None,
    expected_values: Optional[Dict[str, str]] = None,
    source_name: str = "",
) -> Dict[str, Any]:
    """Process a single standalone XLSX/CSV document and return the full
    result, shaped exactly like `pipeline.expediente.process_pdf`'s /
    `pipeline.xml_document.process_xml`'s return value."""
    started = time.time()
    started_at = datetime.now(timezone.utc)
    processing_id = str(uuid.uuid4())
    expected_values = expected_values or {}

    def _fail(
        message: str, *, stage: Optional[str] = None, exception: Optional[BaseException] = None
    ) -> Dict[str, Any]:
        return error_result(
            processing_id, source_name, message,
            build_tracking(started_at, started, _SOURCE_LABEL),
            stage=stage, exception=exception, source_format=source_format,
        )

    if not declared_type:
        return _fail(
            f"{source_format.upper()} documents require an explicit declared type "
            f"(upload under raw/{{TYPE}}/{{uuid}}/file.{source_format}) — there is no "
            "vision-based auto-classification for raw tabular data",
            stage="declared_type",
        )

    entry = get_type_entry(declared_type)
    if not entry.field_tree:
        return _fail(
            f"type '{declared_type}' has no field-centric schema (\"fields\") "
            "to drive tabular extraction",
            stage="declared_type",
        )

    try:
        grid = parse_grid(data, source_format)
    except Exception as e:
        logger.exception("failed to parse %s for %s", source_format, declared_type)
        return _fail(f"invalid {source_format}: {e}", stage="parse", exception=e)

    if not grid or not next(iter(grid.values()), []):
        return _fail(f"{source_format} document has no readable rows", stage="parse")

    entity_map, confidences = extract_from_grid(grid, entry)

    liq = apply_liquidacion_normalizada(entity_map, confidences, entry)
    # Config-driven derived fields (`derived` blocks in the catalog) — same
    # convention and same ordering as the PDF pipeline; a no-op for a type
    # that declares none. See catalogs/README.md.
    derived = apply_derived_fields(entity_map, confidences, entry)

    ctx = ValidationContext(expected_values=expected_values, sibling_documents=[], document_text="")
    resolved_tree, checks, effective_confidences = engine.resolve_field_tree(
        entity_map, confidences, entry, ctx, default_source="TABULAR_EXTRACTION",
        source_overrides=derived.sources, value_labels=derived.labels,
        default_confidence=_TABULAR_CONFIDENCE,
    )
    status = engine.document_status_from_checks(checks)
    confidence_summary = conf_policy.summarize(effective_confidences.values())

    doc: Dict[str, Any] = {
        "documentType": declared_type,
        "documentSubtype": None,
        "pageStart": 1,
        "pageEnd": 1,
        "classificationConfidence": _TABULAR_CONFIDENCE,
        "extractedEntities": resolved_tree,
        "fields": {},
        "checks": checks_to_json(checks),
        "status": status,
        "businessDestination": entry.business_destination or None,
        "confidence": _TABULAR_CONFIDENCE,
        "fieldTreeShape": True,
        **(
            {"fieldConfidence": confidence_summary.shared}
            if confidence_summary.shared is not None else {}
        ),
        **({"liquidacionNormalizada": liq} if liq is not None else {}),
    }

    tracking = build_tracking(started_at, started, _SOURCE_LABEL, doc_types=[declared_type])
    return {
        "processingId": processing_id,
        "sourceFile": source_name,
        "processedAt": tracking["processingCompletedAt"],
        "processingSeconds": tracking["processingSeconds"],
        "processingMs": tracking["processingMs"],
        "tracking": tracking,
        "modelIds": {},
        "sourceFormat": source_format,
        "pages": {"totalPages": 1, "sheets": len(grid)},
        "status": status,
        "reviewRequired": status in (engine.DOC_REVIEW, engine.DOC_PARTIAL),
        "documents": [doc],
        "crossDocumentChecks": [],
        "warnings": [],
    }
