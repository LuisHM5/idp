"""Los reportes deben mostrar lo que la selección de páginas ahorró.

Dos trampas concretas que estos tests vigilan, ambas ya documentadas en
`_report_lib.normalize_cost_item`:

1. **Decimal.** DynamoDB devuelve TODO número como `Decimal`, y `reporte.py`
   embebe las filas con `json.dumps`, que no sabe serializarlo. Un fixture con
   ints simples escondería el fallo — exactamente como pasó una vez — así que
   aquí los datos entran como `Decimal`, igual que en producción.
2. **Sumar lo estimado al costo real.** `estimated_cost_avoided` valora una
   llamada que nunca se hizo; si entrara en `total_cost`, los reportes
   sobrestimarían el gasto y la cifra dejaría de servir para presupuestar.
"""

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _report_lib import normalize_cost_item, normalize_doc_item  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Tal como sale de DynamoDB: cada número es un Decimal.
PAGE_SELECTION = {
    "pagesConsidered": Decimal(86), "pagesExtracted": Decimal(16), "pagesSkipped": Decimal(70),
    "charsSent": Decimal(50109), "charsSkipped": Decimal(105096),
    "chunksSent": Decimal(2), "estimatedChunksWithoutSelection": Decimal(6),
    "billedInputTokens": Decimal(18719), "billedOutputTokens": Decimal(1800),
    "estimatedInputTokensSaved": Decimal(39260), "estimatedOutputTokensSaved": Decimal(3600),
    "estimatedInputSavingPct": Decimal("67.7"), "estimatedCostAvoidedUsd": Decimal("0.062986"),
}
COST_ITEM = {
    "DocumentId": "doc-abc", "DocumentType": "PEDIMENTO_ADUANAL",
    "DocumentTypeName": "Pedimento Aduanal", "ProcessedAt": "2026-08-28T10:00:00Z",
    "Pages": {"Total": Decimal(86), "Native": Decimal(67), "Ocr": Decimal(19), "Empty": Decimal(0)},
    "Services": {"BedrockExtraction": {
        "costUsd": Decimal("0.030491"), "inputTokens": Decimal(18719),
        "outputTokens": Decimal(1800), "modelId": "us.anthropic.claude-haiku-4-5",
    }},
    "TotalCostUsd": Decimal("0.030491"), "TotalInputTokens": Decimal(18719),
    "TotalOutputTokens": Decimal(1800), "PageSelection": PAGE_SELECTION,
}
DOC_ITEM = {
    "DocumentId": "doc-abc", "DocumentType": "PEDIMENTO_ADUANAL",
    "ValidationStatus": "APROBADO", "ProcessedAt": "2026-08-28T10:00:00Z",
}


def test_normalize_casts_every_decimal_away():
    ps = normalize_cost_item(COST_ITEM)["page_selection"]
    assert not [k for k, v in ps.items() if isinstance(v, Decimal)]
    assert ps["chars_skipped"] == 105096
    assert ps["input_saving_pct"] == 67.7
    # Si esto lanza, el HTML de reporte.py no se puede generar.
    json.dumps(ps)


def test_absent_when_selection_never_ran():
    assert normalize_cost_item({"DocumentId": "x"})["page_selection"] is None


def test_reporte_costos_aggregates_and_renders():
    rc = _load("rc", "reporte-costos.py")
    rows = [rc.normalize_item(COST_ITEM)]
    agg = rc.aggregate_page_selection(rows)

    assert agg["input_tokens_saved"] == 39260
    assert agg["cost_without_selection"] == agg["billed_cost"] + agg["cost_avoided"]

    overall = rc.aggregate_overall(rows)
    md = rc.render_markdown(rows, overall, rc.aggregate_by_type(rows), rc.aggregate_by_model(rows),
                            rc.aggregate_by_service(rows), rc.top_expensive(rows, 5), "dev", agg)
    assert "Ahorro por selección de páginas" in md
    # El costo total del reporte es SOLO lo facturado.
    assert overall["total_cost"] == 0.030491


def test_reporte_costos_omits_the_section_when_nothing_was_filtered():
    rc = _load("rc", "reporte-costos.py")
    rows = [rc.normalize_item({k: v for k, v in COST_ITEM.items() if k != "PageSelection"})]
    assert rc.aggregate_page_selection(rows) is None
    md = rc.render_markdown(rows, rc.aggregate_overall(rows), rc.aggregate_by_type(rows),
                            rc.aggregate_by_model(rows), rc.aggregate_by_service(rows),
                            rc.top_expensive(rows, 5), "dev", None)
    assert "Ahorro por selección" not in md


def test_reporte_dashboard_carries_it_through_the_join():
    rp = _load("rp", "reporte.py")
    rows = rp.join_rows([normalize_cost_item(COST_ITEM)], [normalize_doc_item(DOC_ITEM)])

    assert rows[0]["pageSelection"] is not None
    # El dashboard embebe las filas con json.dumps.
    json.dumps(rows[0])

    snapshot = rp.overall_snapshot(rows)["page_selection"]
    assert snapshot["pages_skipped"] == 70
    assert snapshot["cost_avoided"] > 0

    html = rp.render_html(rows, "dev")
    assert 'id="page-sel-card"' in html
    assert "pageSelAgg" in html and "renderPageSel" in html


def test_dashboard_snapshot_is_none_without_selection():
    """La tarjeta se esconde en vez de pintar una fila de ceros."""
    rp = _load("rp", "reporte.py")
    rows = rp.join_rows(
        [normalize_cost_item({k: v for k, v in COST_ITEM.items() if k != "PageSelection"})],
        [normalize_doc_item(DOC_ITEM)],
    )
    assert rp.overall_snapshot(rows)["page_selection"] is None
