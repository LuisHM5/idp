"""Las tarifas tienen que cubrir los modelos que el pipeline REALMENTE puede correr.

El bug que fija este archivo: `_BEDROCK_PRICING` tenía un solo modelo (Haiku
4.5) mientras `model_fallback.DEFAULT_FALLBACK_MODELS` ponía `minimax.minimax-m2.5`
de PRIMERO. O sea, el modelo que corría en producción no estaba en la tabla y
cada registro de costo se calculaba a tarifa de Haiku: un expediente real de 31
páginas se reportaba como $0.143 cuando costó $0.070 — 2x de más, y hasta 4x en
la parte de Bedrock.

Lo insidioso es que el fallback de tarifa es SILENCIOSO por diseño (devuelve un
número plausible, solo marcado con `approximatePricing`), y el fallback de
MODELO también lo es: `_invoke_with_fallback` cambia de modelo solo cuando el
configurado no está disponible. Los dos mecanismos son correctos por separado;
juntos hacen que nadie note que se está cobrando con la tarifa equivocada.

Por eso el test no es "minimax cuesta X" (eso envejece con cada cambio de
precio de AWS) sino la INVARIANTE: todo modelo que el pipeline puede llegar a
invocar solo tiene que tener tarifa propia.
"""

import sys
from pathlib import Path

import pytest

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

from pipeline import pricing  # noqa: E402
from pipeline.model_fallback import DEFAULT_FALLBACK_MODELS  # noqa: E402


@pytest.mark.parametrize("model_id", DEFAULT_FALLBACK_MODELS)
def test_every_fallback_model_has_a_real_rate(model_id):
    """Si agregas un modelo a la lista de fallback, agrégalo también a
    `_BEDROCK_PRICING` — si no, todo lo que corra con él se cobra a la tarifa
    de otro modelo sin más aviso que un flag que nadie mira."""
    assert model_id in pricing._BEDROCK_PRICING, (
        f"{model_id} puede correr (está en DEFAULT_FALLBACK_MODELS) pero no tiene "
        f"tarifa propia: sus costos se reportarían a tarifa de "
        f"{pricing._DEFAULT_BEDROCK_MODEL}"
    )


def test_a_known_model_is_priced_exactly_and_an_unknown_one_is_flagged():
    usage = {"inputTokens": 1_000_000, "outputTokens": 1_000_000}

    known = pricing.bedrock_cost(usage, "minimax.minimax-m2.5")
    assert "approximatePricing" not in known
    # Verificado 2026-09-04 contra la AWS Price List API (AmazonBedrock,
    # us-east-1, service_tier=standard, effectiveDate 2026-08-01).
    assert (known["inputRatePerMTokUsd"], known["outputRatePerMTokUsd"]) == (0.30, 1.20)
    assert known["costUsd"] == pytest.approx(1.50)

    unknown = pricing.bedrock_cost(usage, "proveedor.modelo-que-nadie-registro")
    assert unknown["approximatePricing"] is True


def test_every_rate_entry_has_the_four_keys_bedrock_cost_reads():
    """`bedrock_cost` lee las cuatro sin `.get`: una entrada incompleta sería un
    KeyError en runtime dentro del registro de costos, que es best-effort y se
    tragaría el error dejando el documento sin costo."""
    for model_id, rates in pricing._BEDROCK_PRICING.items():
        assert set(rates) == {"input", "output", "cacheRead", "cacheWrite"}, model_id
        assert all(isinstance(v, (int, float)) and v > 0 for v in rates.values()), model_id


def test_the_default_model_is_itself_priced():
    assert pricing._DEFAULT_BEDROCK_MODEL in pricing._BEDROCK_PRICING
