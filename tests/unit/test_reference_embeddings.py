"""Embeddings de referencias: la abstracción de proveedor y el índice vectorial.

Sin AWS. Lo que se fija:

- el preprocesado es el MISMO con más o menos papel alrededor del elemento;
- solo se comparan vectores del mismo tamaño (modelos distintos nunca se
  parecen por accidente);
- un proveedor desconocido apaga los embeddings, no truena ni cambia de modelo;
- cambiar de modelo es registrar una clase: nada más conoce un modelo;
- la petición y la respuesta de Titan tienen la forma que Bedrock espera;
- el índice convierte distancia a similitud en un solo lugar.
"""

import base64
import io
import json
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src/document-processing/resources/enrichment-lambda"))

from pipeline import pricing, reference_embeddings as emb, reference_index  # noqa: E402


def sello(margen=20, semilla=3):
    """Un 'sello' azul sobre papel, con `margen` pixeles de papel alrededor."""
    rng = np.random.default_rng(semilla)
    tinta = np.where(rng.random((90, 160, 1)) > 0.5, (40, 60, 190), 245).astype(np.uint8)
    hoja = np.full((90 + 2 * margen, 160 + 2 * margen, 3), 245, dtype=np.uint8)
    hoja[margen:margen + 90, margen:margen + 160] = tinta
    return hoja


def dimensiones(jpeg):
    from PIL import Image

    return Image.open(io.BytesIO(jpeg)).size


class ProveedorFalso:
    name, model_id, dimension = "falso", "modelo-falso", 4

    def __init__(self):
        self.llamadas = 0

    def embed_image(self, jpeg):
        self.llamadas += 1
        return np.array([3.0, 0.0, 4.0, 0.0], dtype=np.float32)


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    for nombre in ("REFERENCE_EMBEDDINGS_ENABLED", "REFERENCE_EMBEDDING_PROVIDER",
                   "REFERENCE_VECTOR_BUCKET", "REFERENCE_VECTOR_INDEX"):
        monkeypatch.delenv(nombre, raising=False)
    emb._INSTANCES.clear()
    yield
    emb._INSTANCES.clear()


def test_el_mismo_elemento_con_mas_papel_alrededor_da_la_misma_imagen():
    justo, holgado = emb.prepare(sello(margen=6)), emb.prepare(sello(margen=120))

    ancho_a, alto_a = dimensiones(justo)
    ancho_b, alto_b = dimensiones(holgado)
    assert abs(ancho_a - ancho_b) <= 2 and abs(alto_a - alto_b) <= 2
    assert max(ancho_a, alto_a) <= 512


def test_una_imagen_sin_tinta_se_prepara_entera_y_una_ilegible_no_truena():
    assert emb.prepare(np.full((100, 200, 3), 250, dtype=np.uint8)) is not None
    assert emb.prepare(b"no soy una imagen") is None
    assert emb.embed(b"no soy una imagen", ProveedorFalso()) == (None, None)


def test_embed_normaliza_y_reporta_lo_facturado():
    proveedor = ProveedorFalso()

    vector, meta = emb.embed(sello(), proveedor)

    assert np.allclose(vector, [0.6, 0.0, 0.8, 0.0]) and proveedor.llamadas == 1
    assert meta == {"provider": "falso", "modelId": "modelo-falso", "dimension": 4, "images": 1}


def test_un_fallo_del_modelo_es_sin_vector_nunca_una_excepcion():
    class Roto(ProveedorFalso):
        def embed_image(self, jpeg):
            raise RuntimeError("AccessDeniedException")

    assert emb.embed(sello(), Roto()) == (None, None)
    assert emb.embed(sello(), None) == (None, None)


def test_coseno_entre_vectores_guardados_y_nunca_entre_modelos_de_distinto_tamano():
    a = emb.normalize([1, 0, 0, 0])
    assert emb.cosine(a, emb.to_bytes(a)) == 1.0
    assert emb.cosine(a, emb.normalize([0, 1, 0, 0])) == 0.0
    assert emb.cosine(a, emb.normalize([1, 0, 0])) == 0.0      # otro modelo, otra dimensión
    assert emb.cosine(a, None) == 0.0 and emb.cosine(None, a) == 0.0


def test_el_proveedor_por_defecto_es_titan_y_uno_desconocido_apaga_los_embeddings(monkeypatch):
    assert emb.provider_for(None).model_id == "amazon.titan-embed-image-v1"
    assert emb.provider_for({"embedding": {"provider": "no-existe"}}) is None
    monkeypatch.setenv("REFERENCE_EMBEDDINGS_ENABLED", "false")
    assert emb.provider_for(None) is None


def test_cambiar_de_modelo_es_registrar_una_clase(monkeypatch):
    monkeypatch.setitem(emb.PROVIDERS, "falso", ProveedorFalso)

    por_catalogo = emb.provider_for({"embedding": {"provider": "falso"}})
    monkeypatch.setenv("REFERENCE_EMBEDDING_PROVIDER", "falso")
    por_entorno = emb.provider_for(None)

    assert por_catalogo.model_id == "modelo-falso" and por_entorno is por_catalogo
    assert reference_index.index_name_for("references", por_catalogo) == "references-falso-4"


def test_titan_manda_y_lee_la_forma_que_bedrock_espera():
    class Cliente:
        def invoke_model(self, **kwargs):
            self.kwargs = kwargs
            return {"body": io.BytesIO(json.dumps({"embedding": [0.5] * 1024, "inputTextTokenCount": 0}).encode())}

    cliente = Cliente()
    vector = emb.TitanMultimodalProvider(cliente).embed_image(b"\xff\xd8\xffjpeg")

    cuerpo = json.loads(cliente.kwargs["body"])
    assert cliente.kwargs["modelId"] == "amazon.titan-embed-image-v1"
    assert base64.b64decode(cuerpo["inputImage"]) == b"\xff\xd8\xffjpeg"
    assert cuerpo["embeddingConfig"] == {"outputEmbeddingLength": 1024}
    assert vector.shape == (1024,) and vector.dtype == np.float32


def test_titan_rechaza_una_respuesta_de_otra_dimension():
    class Cliente:
        def invoke_model(self, **_):
            return {"body": io.BytesIO(json.dumps({"embedding": [0.5] * 384}).encode())}

    with pytest.raises(ValueError):
        emb.TitanMultimodalProvider(Cliente()).embed_image(b"x")


def test_todo_proveedor_registrado_tiene_tarifa_propia():
    """Mismo invariante que `test_pricing_coverage`: un modelo sin tarifa se
    cobraría a la de otro sin más aviso que un flag."""
    for nombre, clase in emb.PROVIDERS.items():
        assert clase.model_id in pricing._EMBEDDING_PRICING, nombre
    assert pricing.embedding_cost(3, "amazon.titan-embed-image-v1")["costUsd"] == pytest.approx(0.00018)
    assert pricing.embedding_cost(0, None)["costUsd"] == 0
    assert pricing.embedding_cost(1, "otro.modelo")["approximatePricing"] is True


# ---------------------------------------------------------------- índice


class S3VectorsFalso:
    def __init__(self, respuesta=None, falla=False):
        self.respuesta, self.falla, self.llamadas = respuesta or {"vectors": []}, falla, []

    def _hacer(self, nombre, kwargs):
        if self.falla:
            raise RuntimeError("s3vectors caído")
        self.llamadas.append((nombre, kwargs))

    def put_vectors(self, **kwargs):
        self._hacer("put", kwargs)

    def delete_vectors(self, **kwargs):
        self._hacer("delete", kwargs)

    def query_vectors(self, **kwargs):
        self._hacer("query", kwargs)
        return self.respuesta


def test_el_indice_guarda_con_metadatos_filtrables_y_borra_por_la_misma_llave():
    cliente = S3VectorsFalso()
    indice = reference_index.S3VectorsIndex("cubeta", "references-x-4", client=cliente)

    assert indice.put("PROVEEDOR", "10040", "SELLO", "planta", emb.normalize([1, 0, 0, 0]), "m1") is True
    assert indice.delete("PROVEEDOR", "10040", "SELLO", "planta") is True

    _, puesto = cliente.llamadas[0]
    assert puesto["vectorBucketName"] == "cubeta" and puesto["indexName"] == "references-x-4"
    assert puesto["vectors"][0]["key"] == "PROVEEDOR#10040#SELLO#planta"
    assert puesto["vectors"][0]["metadata"] == {"scope": "PROVEEDOR", "key": "10040", "kind": "SELLO",
                                                "refId": "planta", "model": "m1"}
    assert cliente.llamadas[1][1]["keys"] == ["PROVEEDOR#10040#SELLO#planta"]


def test_la_busqueda_filtra_por_scope_elemento_y_modelo_y_devuelve_similitud_no_distancia():
    cliente = S3VectorsFalso({"vectors": [
        {"key": "k2", "distance": 0.40, "metadata": {"key": "10021", "refId": "b"}},
        {"key": "k1", "distance": 0.05, "metadata": {"key": "10040", "refId": "a"}},
        {"key": "k3", "distance": 0.10, "metadata": {}},   # sin metadatos: no se puede atribuir
    ]})
    indice = reference_index.S3VectorsIndex("cubeta", "idx", client=cliente)

    hallazgos = indice.search(emb.normalize([1, 0, 0, 0]), scope="PROVEEDOR", kind="SELLO", top_k=3, model="m1")

    assert hallazgos == [{"key": "10040", "refId": "a", "similarity": 0.95},
                         {"key": "10021", "refId": "b", "similarity": 0.6}]
    _, consulta = cliente.llamadas[0]
    assert consulta["filter"] == {"$and": [{"scope": {"$eq": "PROVEEDOR"}}, {"kind": {"$eq": "SELLO"}},
                                           {"model": {"$eq": "m1"}}]}
    assert consulta["returnDistance"] is True and consulta["topK"] == 3


def test_un_error_del_indice_es_no_se_pudo_buscar_nunca_un_crash():
    indice = reference_index.S3VectorsIndex("cubeta", "idx", client=S3VectorsFalso(falla=True))

    assert indice.search([1.0], scope="P", kind="SELLO", top_k=1, model="m") is None
    assert indice.put("P", "k", "SELLO", "r", [1.0], "m") is False
    assert indice.delete("P", "k", "SELLO", "r") is False


def test_sin_configurar_no_hay_indice(monkeypatch):
    proveedor = ProveedorFalso()
    assert reference_index.default_index(proveedor) is None
    monkeypatch.setenv("REFERENCE_VECTOR_BUCKET", "cubeta")
    monkeypatch.setenv("REFERENCE_VECTOR_INDEX", "references")
    assert reference_index.default_index(None) is None
    assert reference_index.default_index(proveedor).index == "references-falso-4"
