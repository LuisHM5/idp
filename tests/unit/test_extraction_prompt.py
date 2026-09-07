"""Los hints del catálogo tienen que llegar al modelo SIEMPRE.

`_build_field_tree_prompts` arma un prefijo con el bloque CAMPOS (nombre +
hint de cada campo) y las REGLAS ESTRICTAS. Ese prefijo viajaba únicamente en
`cache_system`, y `cache_system` solo se mandaba cuando el documento se partía
en más de un chunk — así que un pedimento de 2 páginas (un solo chunk) se
extraía SIN hints y SIN reglas: el modelo recibía el texto de las páginas y los
nombres de campo del tool schema, y llenaba cada uno adivinando qué significaba
el nombre. Medido sobre test-docs/pedimentos-screens/2607-0973-6100971_P.PDF-1.pdf:
el acuse "COVE2689JP013 / 35079805" acababa en `Factura`, `num_acuse_valor`
salía null y `Valor_ComercialDol` traía el valor en PESOS.

El contrato que fija este test: el prefijo va en todas las llamadas; `cache`
solo decide si además se marca como cacheable.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = REPO_ROOT / "src/document-processing/resources/enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from catalog_paths import active_catalog_path  # noqa: E402
from document_catalog import _parse_entry  # noqa: E402
from pipeline import text_extractor  # noqa: E402
from pipeline.pdf_pages import PageText  # noqa: E402

# Las dos páginas del pedimento de TYSON FOODS caben en un solo chunk.
PAGINAS = [
    PageText(1, "NUM. PEDIMENTO: 26 07 0973 6100971\n"
                "NUMERO DE ACUSE DE VALOR   FECHA   INCOTERM\n"
                "COVE2689JP013 / 35079805   24/08/2026   CPT", "native", 0.98),
]


def cargar(type_id):
    path = active_catalog_path(REPO_ROOT, type_id)
    return _parse_entry(type_id, json.loads(path.read_text(encoding="utf-8"))[type_id])


@pytest.fixture
def llamadas(monkeypatch):
    """Captura los kwargs de cada llamada a Bedrock sin hacer ninguna."""
    capturadas = []

    def fake_converse(prompt, **kwargs):
        capturadas.append({"prompt": prompt, **kwargs})
        return {}, {"usage": {}}

    monkeypatch.setattr(text_extractor, "converse_structured", fake_converse)
    monkeypatch.setattr(text_extractor, "resolve_model", lambda *a, **k: "modelo-de-prueba")
    return capturadas


def instrucciones(llamada):
    """Lo que el modelo recibe como system, se marque cacheable o no."""
    return (llamada.get("system") or "") + (llamada.get("cache_system") or "")


def test_un_documento_de_un_solo_chunk_recibe_los_hints(llamadas):
    text_extractor.extract_document(PAGINAS, cargar("PEDIMENTO_ERP"))

    assert len(llamadas) == 1, "un solo chunk = una sola llamada"
    enviado = instrucciones(llamadas[0])
    assert "CAMPOS:" in enviado
    assert "num_acuse_valor" in enviado
    assert "NUMERO DE ACUSE DE VALOR" in enviado          # el hint completo, no solo el nombre
    assert "REGLAS ESTRICTAS" in enviado


def test_un_solo_chunk_no_paga_cache_pero_manda_las_instrucciones(llamadas):
    """Cachear un prefijo que nadie va a reusar es gasto puro: con un chunk
    va como `system` normal. Lo que no puede pasar es que no vaya."""
    text_extractor.extract_document(PAGINAS, cargar("PEDIMENTO_ERP"))

    assert llamadas[0].get("cache_system") is None
    assert "CAMPOS:" in (llamadas[0].get("system") or "")


def test_con_varios_chunks_el_prefijo_se_marca_cacheable(llamadas, monkeypatch):
    """Con >1 chunk el mismo prefijo se reusa en cada llamada: ahí sí paga."""
    monkeypatch.setattr(text_extractor, "_chunk_pages", lambda pages: [PAGINAS, PAGINAS])

    text_extractor.extract_document(PAGINAS, cargar("PEDIMENTO_ERP"))

    assert len(llamadas) == 2
    for llamada in llamadas:
        assert "CAMPOS:" in (llamada.get("cache_system") or "")
        assert llamada.get("system") is None
