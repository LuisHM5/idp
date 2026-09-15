"""Estimado de costo antes de procesar (`scripts/_cost_estimate.py`) y su
calibración contra costos reales (`scripts/costos-validar.py`).

El estimado tiene que calcularse como cobra el pipeline — modelo resuelto,
páginas OCR, selección, llamadas y tokens del prompt real — y nunca puede
disparar una llamada pagada. Todo offline: PDFs generados en el momento y el
catálogo leído del JSON del repo."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfWriter

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src/document-processing/resources/enrichment-lambda"))

import _cost_estimate as ce  # noqa: E402
from document_catalog import _parse_entry  # noqa: E402

spec = importlib.util.spec_from_file_location("costos_validar_under_test", REPO / "scripts/costos-validar.py")
validar = importlib.util.module_from_spec(spec)
sys.modules["costos_validar_under_test"] = validar
spec.loader.exec_module(validar)

MINIMAX, HAIKU = "minimax.minimax-m2.5", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
CALIBRATION = {
    "default": {"charsPerToken": 3.0, "ocrCharsPerPage": 2000, "outputTokensPerCall": {"median": 1000, "p90": 2000}},
    "models": {MINIMAX: {"charsPerToken": 3.3, "supportsPromptCache": False,
                         "outputTokensPerCall": {"median": 1500, "p90": 2500},
                         "outputTokensPerCallByType": {"PEDIMENTO_ERP": {"median": 2200, "p90": 3500}}},
               HAIKU: {"charsPerToken": 3.5, "supportsPromptCache": True, "outputTokensPerCall": {"median": 2000, "p90": 3000}}},
    "lambda": {"baseSeconds": 5, "secondsPerOcrPage": 1, "secondsPerCall": 10, "deterministicSeconds": 2},
    "ocrCharsPerPageByType": {"PEDIMENTO_ERP": {"median": 2600}},
    "selectedScannedPagesByType": {"PEDIMENTO_ERP": {"median": 3}},
}


def erp_entry():
    data = json.loads((REPO / "catalogs/custom/erp/PEDIMENTO_ERP.json").read_text(encoding="utf-8"))["PEDIMENTO_ERP"]
    return _parse_entry("PEDIMENTO_ERP", data)


def deployment(model=MINIMAX, **variables):
    return ce.Deployment(env="dev", function="fn", memory_mb=1024, variables={"BEDROCK_MODEL_ID": model, **variables},
                         source="lambda")


def blank_pdf(path: Path, pages: int) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(612, 792)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def estimate(path: Path, entry=None, dep=None, calibration=CALIBRATION):
    dep = dep or deployment()
    with ce.pipeline_context(dep) as mods:
        document_catalog = mods[0]
        document_catalog._CATALOG_CACHE = {"PEDIMENTO_ERP": entry or erp_entry()}
        return ce.estimate_file(path, "PEDIMENTO_ERP", dep, calibration, mods)


def test_paid_clients_are_blocked_and_environment_is_restored() -> None:
    os.environ["BEDROCK_MODEL_ID"] = "valor-previo"
    try:
        with ce.pipeline_context(deployment(MINIMAX, CHUNK_WORKERS="4")) as mods:
            _, bedrock, _, _, pdf_pages, _, _, _ = mods
            assert os.environ["BEDROCK_MODEL_ID"] == MINIMAX
            with pytest.raises(ce.PaidCallBlocked):
                bedrock.client()
            with pytest.raises(ce.PaidCallBlocked):
                pdf_pages._textract_client()
        assert os.environ["BEDROCK_MODEL_ID"] == "valor-previo"
        assert "CHUNK_WORKERS" not in os.environ or os.environ["CHUNK_WORKERS"] != "4"
    finally:
        os.environ.pop("BEDROCK_MODEL_ID", None)


def test_scanned_pdf_textract_is_exact_and_selection_uses_observed_pages(tmp_path: Path) -> None:
    result = estimate(blank_pdf(tmp_path / "scan.pdf", 31))
    assert result["model"] == MINIMAX
    assert (result["pages"], result["ocrPages"]) == (31, 31)
    assert result["textractUsd"] == pytest.approx(31 * 0.0015)
    # 31 escaneadas > expected_pages del tipo: medio = las 3 que la selección suele elegir; alto = todas.
    assert (result["extractPages"], result["extractPagesHigh"]) == (3, 31)
    assert result["exact"] is False
    assert result["outputTokens"] == 2200 * result["calls"]  # calibración por tipo
    assert result["totalHighUsd"] > result["totalUsd"] > result["textractUsd"]
    assert result["totalUsd"] == pytest.approx(result["textractUsd"] + result["bedrockUsd"] + result["lambdaUsd"], abs=1e-6)


def test_only_pages_without_text_layer_are_billed_to_textract(tmp_path: Path, monkeypatch) -> None:
    pdf = blank_pdf(tmp_path / "mixed.pdf", 3)
    dep = deployment()
    with ce.pipeline_context(dep) as mods:
        document_catalog, _, _, _, pdf_pages, _, _, _ = mods
        document_catalog._CATALOG_CACHE = {"PEDIMENTO_ERP": erp_entry()}
        native = "NUM. PEDIMENTO 26 07 3435 6041353 " + "texto " * 400
        monkeypatch.setattr(pdf_pages, "load_pages", lambda _data, use_ocr=True: [
            pdf_pages.PageText(1, native, "native", 0.98), pdf_pages.PageText(2, native, "native", 0.98),
            pdf_pages.PageText(3, "", "empty", 0.0)])
        result = ce.estimate_file(pdf, "PEDIMENTO_ERP", dep, CALIBRATION, mods)
    assert (result["pages"], result["ocrPages"]) == (3, 1)
    assert result["textractUsd"] == pytest.approx(0.0015)
    # El texto nativo cuenta caracteres reales: más entrada que 3 páginas vacías.
    assert result["inputTokens"] > round((2 * len(native)) / 3.3)


def test_catalog_model_override_wins_over_deployment_and_is_priced_at_its_rate(tmp_path: Path) -> None:
    pdf = blank_pdf(tmp_path / "two.pdf", 2)
    cheap = estimate(pdf)
    pinned = estimate(pdf, entry=replace(erp_entry(), model_id=HAIKU))
    assert pinned["model"] == HAIKU
    assert pinned["textractUsd"] == cheap["textractUsd"]
    assert pinned["bedrockUsd"] > cheap["bedrockUsd"] * 2  # Haiku cuesta varias veces lo que MiniMax


def test_xml_is_free_of_ai_and_images_are_one_ocr_page(tmp_path: Path) -> None:
    xml = tmp_path / "cfdi.xml"
    xml.write_text("<cfdi/>", encoding="utf-8")
    free = estimate(xml)
    assert free["textractUsd"] == 0 and free["bedrockUsd"] == 0 and free["exact"] is True
    image = tmp_path / "ine.png"
    image.write_bytes(b"\x89PNG")
    one = estimate(image)
    assert (one["pages"], one["ocrPages"]) == (1, 1)
    assert one["textractUsd"] == pytest.approx(0.0015)


def test_calls_follow_the_pipeline_chunking_and_schema_partitions() -> None:
    with ce.pipeline_context(deployment()) as mods:
        _, _, _, _, pdf_pages, _, _, text_extractor = mods
        entry = erp_entry()
        pages = [pdf_pages.PageText(n, "x" * 20_000, "native", 0.98) for n in (1, 2, 3)]
        calls = ce._calls_for(mods, entry, pages)
        partitions = len(text_extractor._field_tree_call_partitions(entry.field_tree))
        assert len(calls) == len(text_extractor._chunk_pages(pages)) * partitions == 3 * partitions
        prefix, _ = text_extractor._build_field_tree_prompts(entry, pages[:1])
        assert calls[0]["prefixChars"] == len(prefix)
        assert calls[0]["userChars"] > 20_000


def test_prompt_cache_only_for_anthropic_multi_chunk() -> None:
    calls = [{"prefixChars": 7000, "userChars": 3000, "toolChars": 1000}] * 6
    with ce.pipeline_context(deployment()) as mods:
        minimax = ce._price_calls(mods, calls, 6, MINIMAX, CALIBRATION["models"][MINIMAX], "X", workers=4)["mid"]
        haiku = ce._price_calls(mods, calls, 6, HAIKU, CALIBRATION["models"][HAIKU], "X", workers=4)["mid"]
        single = ce._price_calls(mods, calls[:1], 1, HAIKU, CALIBRATION["models"][HAIKU], "X", workers=4)["mid"]
    assert minimax["cacheWriteTokens"] == minimax["cacheReadTokens"] == 0
    assert haiku["cacheWriteTokens"] == 4 * round(7000 / 3.5)  # la primera tanda en paralelo escribe
    assert haiku["cacheReadTokens"] == 2 * round(7000 / 3.5)   # el resto lee
    assert single["cacheWriteTokens"] == 0                      # un solo chunk: el pipeline no marca cachePoint


def rec(**kwargs):
    base = {"model": MINIMAX, "docType": "PEDIMENTO_ERP", "file": "a.pdf", "inputTokens": 1000, "outputTokens": 3000,
            "calls": 2, "ocrPages": 0, "lambdaSeconds": 0,
            "structure": {"knownChars": 3300, "ocrExtracted": 0, "partitions": 1, "calls": 2,
                          "ocrCharsPerPageExact": None, "selectedScanned": None}}
    base.update(kwargs)
    return base


def test_fit_prefers_exact_ocr_counts_and_discards_implausible_token_derived_ones() -> None:
    records = [
        rec(file="native.pdf"),
        rec(file="scan.pdf", ocrPages=31, inputTokens=2727, structure={"knownChars": 9000, "ocrExtracted": 0, "partitions": 1, "calls": 1,
                                                      "ocrCharsPerPageExact": 2628.0, "selectedScanned": 3}),
        rec(file="old-prompt.pdf", inputTokens=90000, structure={"knownChars": 3000, "ocrExtracted": 2, "partitions": 1,
                                                                  "calls": 1, "ocrCharsPerPageExact": None, "selectedScanned": None},
            docType="FACTURA_FLETE_EXTRANJERO"),
    ]
    fitted = validar.fit(records, {})
    cal = fitted["models"][MINIMAX]
    assert cal["charsPerToken"] == pytest.approx(3.3, rel=0.01)
    assert fitted["ocrCharsPerPageByType"]["PEDIMENTO_ERP"] == {"median": 2628, "documents": 1, "source": "exacto (selección de páginas)"}
    assert "FACTURA_FLETE_EXTRANJERO" not in fitted["ocrCharsPerPageByType"]  # ~148k chars/pág: prompt de otra versión
    assert fitted["selectedScannedPagesByType"]["PEDIMENTO_ERP"]["median"] == 3
    assert cal["outputTokensPerCall"]["median"] == 1500
    assert cal["supportsPromptCache"] is False and validar.fit([rec(model=HAIKU)], {})["models"][HAIKU]["supportsPromptCache"]


def test_model_alias() -> None:
    assert ce.model_alias(MINIMAX) == "minimax"
    assert ce.model_alias(HAIKU) == "haiku-4.5"
    assert ce.model_alias(None) == "—"


def test_cost_breakdown_from_dynamodb_item() -> None:
    e2e = validar._load_e2e()
    item = {
        "DocumentId": {"S": "d1"}, "TotalCostUsd": {"N": "0.0629"}, "PricingVersion": {"S": "2026-09-04"},
        "Services": {"M": {
            "BedrockExtraction": {"M": {"costUsd": {"N": "0.0157"}, "inputTokens": {"N": "5043"}, "outputTokens": {"N": "1845"},
                                        "modelId": {"S": MINIMAX}}},
            "BedrockSegmentation": {"M": {"costUsd": {"N": "0"}, "inputTokens": {"N": "0"}, "outputTokens": {"N": "0"}}},
            "TextractOcr": {"M": {"costUsd": {"N": "0.0465"}, "pages": {"N": "31"}}},
            "LambdaCompute": {"M": {"costUsd": {"N": "0.0007"}}},
        }},
    }
    breakdown = e2e.cost_breakdown_from_item(item)
    assert breakdown == {"totalUsd": 0.0629, "textractUsd": 0.0465, "bedrockUsd": 0.0157, "lambdaUsd": 0.0007,
                         "inputTokens": 5043, "outputTokens": 1845, "ocrPages": 31, "model": MINIMAX,
                         "pricingVersion": "2026-09-04"}
