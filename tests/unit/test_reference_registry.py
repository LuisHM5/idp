"""El registro de referencias y su API REST (`/references/...`), sin AWS.

Tabla, S3, proveedor de embeddings e índice vectorial son falsos en memoria.
Lo que se fija:

- el CLI y la API pasan por la MISMA revisión y las mismas reglas de duplicado;
- una clave que cambia al normalizarse se rechaza, no se reescribe;
- sin el permiso `AllowReferenceWrite` todo `/references` es 403, y falla cerrado;
- una fila activada NO conserva `ExpiresAt` (el TTL borraría una referencia viva);
- una pendiente nunca se compara ni se lista; confirmar dos veces es idempotente;
- si el índice vectorial falla, la referencia queda activa y `indexed: false`.
"""

import io
import json
import sys
import types
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")
pytest.importorskip("boto3")

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src/document-processing/resources/enrichment-lambda"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import references_api  # noqa: E402
from pipeline import authenticity, fingerprint_store, reference_registry as registro  # noqa: E402

from unit.test_fingerprint_store import TablaFalsa  # noqa: E402

ELEMENTOS = {"FIRMA": {"description": "firma"}, "SELLO": {"description": "sello"},
             "LOGO": {"description": "logo", "fingerprint": False}}
SCOPES = {"PROVEEDOR": ELEMENTOS}


class CondicionFallida(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class TablaRegistro(TablaFalsa):
    """`TablaFalsa` + lo que usa el registro: escrituras condicionales,
    `update_item` (SET/REMOVE) y `delete_item`."""

    def put_item(self, Item, ConditionExpression=None, **_):
        actual = self.items.get((Item["Pk"], Item["Sk"]))
        if ConditionExpression and actual is not None and actual.get("Status") != "PENDING":
            raise CondicionFallida()
        super().put_item(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ConditionExpression=None):
        item = self.items.get((Key["Pk"], Key["Sk"]))
        nombres, valores = ExpressionAttributeNames or {}, ExpressionAttributeValues or {}
        if ConditionExpression:
            campo, valor = (p.strip() for p in ConditionExpression.split("="))
            if item is None or item.get(nombres.get(campo, campo)) != valores[valor]:
                raise CondicionFallida()
        poner, _, quitar = UpdateExpression.partition(" REMOVE ")
        for asignacion in poner.replace("SET ", "", 1).split(","):
            campo, valor = (p.strip() for p in asignacion.split("="))
            item[nombres.get(campo, campo)] = valores[valor]
        for campo in filter(None, (c.strip() for c in quitar.split(","))):
            item.pop(campo, None)

    def delete_item(self, Key):
        self.items.pop((Key["Pk"], Key["Sk"]), None)


class S3Falso:
    def __init__(self):
        self.objetos = {}

    def put_object(self, Bucket, Key, Body, **_):
        self.objetos[Key] = Body

    def head_object(self, Bucket, Key):
        if Key not in self.objetos:
            raise RuntimeError("404")
        return {"ContentLength": len(self.objetos[Key])}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objetos[Key])}

    def delete_object(self, Bucket, Key):
        self.objetos.pop(Key, None)

    def generate_presigned_url(self, operacion, Params, ExpiresIn):
        return f"https://s3.falso/{Params['Key']}?op={operacion}&expira={ExpiresIn}"


class ProveedorFalso:
    name, model_id, dimension = "falso", "modelo-falso", 4

    def embed_image(self, jpeg):
        return np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)


class IndiceFalso:
    def __init__(self):
        self.vectores, self.falla = {}, False

    def put(self, scope, key, kind, ref_id, vector, model):
        if self.falla:
            return False
        self.vectores[(scope, key, kind, ref_id)] = model
        return True

    def delete(self, scope, key, kind, ref_id):
        self.vectores.pop((scope, key, kind, ref_id), None)
        return True


def recorte(semilla=1, ancho=300, alto=150, formato="PNG"):
    """Una 'firma': un trazo negro distinto por semilla sobre papel."""
    from PIL import Image

    rng = np.random.default_rng(semilla)
    hoja = np.full((alto, ancho, 3), 246, dtype=np.uint8)
    y = alto / 2
    for x in range(12, ancho - 12):
        y = float(np.clip(y + rng.normal(0, 5), 12, alto - 12))
        hoja[int(y) - 2:int(y) + 2, x] = 30
    salida = io.BytesIO()
    Image.fromarray(hoja).save(salida, format=formato)
    return salida.getvalue()


@pytest.fixture
def mundo():
    tabla, s3, indice = TablaRegistro(), S3Falso(), IndiceFalso()
    reg = registro.Registry(table=tabla, s3=s3, bucket="cubeta", provider=ProveedorFalso(),
                            index_for=lambda proveedor: indice)
    apps = types.SimpleNamespace(items={
        "key-ok": {"AppName": "ERP", "AllowReferenceWrite": True},
        "key-sin-permiso": {"AppName": "CEDIS", "AllowReferenceWrite": False},
    })
    apps.get_item = lambda Key: ({"Item": apps.items[Key["ApiKeyId"]]} if Key["ApiKeyId"] in apps.items else {})
    deps = references_api.Dependencies(registry=reg, apps_table=apps, scopes=lambda: SCOPES, key_required=True)
    return types.SimpleNamespace(tabla=tabla, s3=s3, indice=indice, reg=reg, apps=apps, deps=deps)


def llamar(mundo, metodo, recurso, ruta=None, cuerpo=None, query=None, api_key="key-ok"):
    evento = {"httpMethod": metodo, "resource": recurso, "pathParameters": ruta or {},
              "queryStringParameters": query, "body": json.dumps(cuerpo) if cuerpo is not None else None,
              "requestContext": {"identity": {"apiKeyId": api_key} if api_key else {}}}
    respuesta = references_api.handle(evento, mundo.deps)
    return respuesta["statusCode"], json.loads(respuesta["body"])


BASE = {"scope": "PROVEEDOR", "key": "10040"}


def alta(mundo, kind="FIRMA", label="Lic. Julio Lemus", datos=None, **confirmar):
    """Las tres llamadas del cliente: reservar, PUT a S3, confirmar."""
    estado, reserva = llamar(mundo, "POST", references_api.BASE, BASE, {"kind": kind, "label": label})
    assert estado == 200, reserva
    llave = reserva["uploadUrl"].split("https://s3.falso/")[1].split("?")[0]
    mundo.s3.objetos[llave] = datos if datos is not None else recorte()
    ruta = {**BASE, "kind": kind, "refId": reserva["refId"]}
    return reserva, llamar(mundo, "POST", references_api.CONFIRM, ruta, confirmar or None)


# ---------------------------------------------------------------- revisión


def test_la_revision_acepta_un_recorte_bueno_y_calcula_su_huella():
    ficha = registro.review(recorte(), ELEMENTOS["FIRMA"])

    assert ficha["errors"] == [] and ficha["format"] == "png"
    assert (ficha["width"], ficha["height"]) == (300, 150) and ficha["grid"]


@pytest.mark.parametrize("datos, elemento, codigo", [
    (b"no soy una imagen", "FIRMA", "NOT_AN_IMAGE"),
    (recorte(ancho=90, alto=60), "FIRMA", "TOO_SMALL"),
    (recorte(ancho=2400, alto=2300), "FIRMA", "TOO_LARGE_DIMENSIONS"),
    (b"\x89PNG\r\n\x1a\n" + b"x" * (registro.MAX_BYTES + 1), "FIRMA", "TOO_LARGE_BYTES"),
])
def test_la_revision_dice_por_que_rechaza(datos, elemento, codigo):
    assert [e["code"] for e in registro.review(datos, ELEMENTOS[elemento])["errors"]] == [codigo]


def test_una_imagen_en_blanco_no_tiene_tinta_y_un_logo_no_la_necesita():
    from PIL import Image

    blanco = io.BytesIO()
    Image.fromarray(np.full((150, 300, 3), 250, dtype=np.uint8)).save(blanco, format="PNG")

    assert [e["code"] for e in registro.review(blanco.getvalue(), ELEMENTOS["FIRMA"])["errors"]] == ["NO_INK"]
    assert registro.review(blanco.getvalue(), ELEMENTOS["LOGO"])["errors"] == []
    # Un logo es ancho y bajito: 100 px de alto pasan sin huella, no con ella.
    assert registro.review(recorte(ancho=400, alto=100), ELEMENTOS["LOGO"])["errors"] == []
    assert registro.review(recorte(ancho=400, alto=100), ELEMENTOS["FIRMA"])["errors"][0]["code"] == "TOO_SMALL"


def test_una_cabecera_que_declara_una_imagen_enorme_se_rechaza_sin_decodificarla(monkeypatch):
    """Un PNG chico en bytes puede declarar decenas de megapixeles."""
    from PIL import Image

    gigante = io.BytesIO()
    Image.new("L", (6000, 3000), 255).save(gigante, format="PNG")
    assert len(gigante.getvalue()) < registro.MAX_BYTES
    monkeypatch.setattr(registro.page_raster, "decode_image",
                        lambda datos: pytest.fail("no debe decodificarse"))

    assert [e["code"] for e in registro.review(gigante.getvalue(), ELEMENTOS["FIRMA"])["errors"]] == ["TOO_LARGE_DIMENSIONS"]


def test_el_formato_real_sale_de_los_bytes_no_de_la_extension():
    ficha = registro.review(recorte(formato="JPEG"), ELEMENTOS["FIRMA"], declared_format="png")

    assert ficha["format"] == "jpeg" and ficha["errors"] == []
    assert ficha["warnings"][0]["code"] == "FORMAT_MISMATCH"


def test_la_clave_que_cambia_al_normalizarse_se_rechaza_con_la_sugerida():
    assert registro.validate_key("10040") == "10040"
    with pytest.raises(registro.RegistryError) as error:
        registro.validate_key("ACME S.A.")
    assert error.value.code == "INVALID_REFERENCE_KEY" and error.value.extra["suggested"] == "ACMES.A."
    with pytest.raises(registro.RegistryError):
        registro.validate_key("  ")


def test_los_scopes_salen_de_la_forma_del_catalogo_no_de_un_type_id():
    """Un tipo cualquiera que declare `references` entra; el elemento puede
    traer su propia clave/scope (una firma es de una persona, no de la empresa)."""
    tipos = [
        types.SimpleNamespace(authenticity={
            "elements": {"LOGO": {"description": "logo"}, "FIRMA": {"description": "f", "references": {"scope": "FIRMANTE"}},
                         "HOLOGRAMA": {"description": "h", "references": False}},
            "references": {"scope": "PROVEEDOR", "key_from": "fields.RFC"}}),
        types.SimpleNamespace(authenticity={"elements": {"SELLO": {"description": "s"}}}),   # sin `references`
        types.SimpleNamespace(authenticity=None),
    ]

    scopes = authenticity.reference_scopes(tipos)

    assert {scope: sorted(kinds) for scope, kinds in scopes.items()} == {"PROVEEDOR": ["LOGO"], "FIRMANTE": ["FIRMA"]}


# ---------------------------------------------------------------- permisos


def test_sin_el_permiso_de_la_app_toda_la_api_es_403(mundo):
    for metodo, recurso in ((m, r) for (r, m) in references_api._ROUTES):
        estado, cuerpo = llamar(mundo, metodo, recurso, {**BASE, "kind": "FIRMA", "refId": "x"},
                                api_key="key-sin-permiso")
        assert (estado, cuerpo["error"]) == (403, "FORBIDDEN"), (metodo, recurso)


def test_falla_cerrado_con_una_key_no_registrada_o_la_tabla_de_apps_caida(mundo):
    assert llamar(mundo, "GET", references_api.BASE, BASE, api_key="key-desconocida")[0] == 403

    def caida(Key):
        raise RuntimeError("DynamoDB caído")

    mundo.apps.get_item = caida
    assert llamar(mundo, "GET", references_api.BASE, BASE)[0] == 403


def test_sin_api_key_solo_pasa_donde_el_stack_no_las_exige(mundo):
    assert llamar(mundo, "GET", references_api.BASE, BASE, api_key=None)[0] == 403
    mundo.deps.key_required = False
    assert llamar(mundo, "GET", references_api.BASE, BASE, api_key=None)[0] == 200


# ---------------------------------------------------------------- alta por API


def test_alta_en_dos_pasos_deja_la_referencia_activa_e_indexada(mundo):
    reserva, (estado, ficha) = alta(mundo)

    assert reserva["status"] == "PENDING" and reserva["uploadMethod"] == "PUT"
    assert reserva["uploadExpiresInSeconds"] == 900 and reserva["refId"] == "lic-julio-lemus"
    assert reserva["confirmUrl"] == "/references/PROVEEDOR/10040/FIRMA/lic-julio-lemus/confirm"
    assert estado == 200 and ficha["status"] == "ACTIVE" and ficha["indexed"] is True
    assert (ficha["width"], ficha["height"], ficha["hasInkFingerprint"]) == (300, 150, True)
    assert ficha["createdBy"] == "ERP" and "inkGrid" not in ficha and "embedding" not in ficha

    fila = mundo.tabla.items[("R#PROVEEDOR#10040", "FIRMA#lic-julio-lemus")]
    # El TTL solo existe para la reserva: si sobreviviera, DynamoDB borraría
    # mañana una referencia viva.
    assert "ExpiresAt" not in fila and "PendingS3Key" not in fila
    assert fila["S3Key"] == "references/PROVEEDOR/10040/FIRMA/lic-julio-lemus.png"
    assert list(mundo.s3.objetos) == [fila["S3Key"]]               # el staging se borró
    assert fila["EmbeddingModel"] == "modelo-falso" and fila["EmbeddingDim"] == 4
    assert mundo.indice.vectores == {("PROVEEDOR", "10040", "FIRMA", "lic-julio-lemus"): "modelo-falso"}
    # Y es exactamente lo que lee el pipeline.
    leidas = fingerprint_store.list_references("PROVEEDOR", "10040", table=mundo.tabla)
    assert [r["refId"] for r in leidas["FIRMA"]] == ["lic-julio-lemus"] and leidas["FIRMA"][0]["embedding"]


def test_una_reserva_sin_confirmar_no_se_lista_ni_se_compara(mundo):
    estado, reserva = llamar(mundo, "POST", references_api.BASE, BASE, {"kind": "SELLO", "label": "Planta"})

    assert estado == 200
    assert mundo.tabla.items[("R#PROVEEDOR#10040", "SELLO#planta")]["ExpiresAt"] > 0
    assert fingerprint_store.list_references("PROVEEDOR", "10040", table=mundo.tabla) == {}
    assert llamar(mundo, "GET", references_api.BASE, BASE)[1]["references"] == []
    # Repetir la reserva (un reintento del cliente) reutiliza la fila.
    assert llamar(mundo, "POST", references_api.BASE, BASE, {"kind": "SELLO", "label": "Planta"})[1]["refId"] == "planta"


def test_confirmar_sin_haber_subido_la_imagen_es_409(mundo):
    _, reserva = llamar(mundo, "POST", references_api.BASE, BASE, {"kind": "FIRMA", "label": "Julio"})

    estado, cuerpo = llamar(mundo, "POST", references_api.CONFIRM, {**BASE, "kind": "FIRMA", "refId": reserva["refId"]})

    assert (estado, cuerpo["error"]) == (409, "UPLOAD_NOT_FOUND")


def test_un_recorte_que_no_pasa_la_revision_es_422_y_no_deja_nada(mundo):
    _, (estado, cuerpo) = alta(mundo, datos=recorte(ancho=90, alto=60))

    assert estado == 422 and cuerpo["error"] == "REFERENCE_REJECTED"
    assert cuerpo["errors"][0]["code"] == "TOO_SMALL"
    assert mundo.s3.objetos == {}
    assert mundo.tabla.items[("R#PROVEEDOR#10040", "FIRMA#lic-julio-lemus")]["Status"] == "PENDING"


def test_un_archivo_enorme_se_rechaza_por_su_tamano_sin_descargarlo(mundo):
    mundo.s3.get_object = lambda **_: pytest.fail("no debe descargarse")

    _, (estado, cuerpo) = alta(mundo, datos=b"x" * (registro.MAX_BYTES + 1))

    assert estado == 422 and cuerpo["errors"][0]["code"] == "TOO_LARGE_BYTES"


def test_confirmar_dos_veces_es_idempotente(mundo):
    reserva, (_, primera) = alta(mundo)

    estado, segunda = llamar(mundo, "POST", references_api.CONFIRM, {**BASE, "kind": "FIRMA", "refId": reserva["refId"]})

    assert estado == 200 and segunda["sha256"] == primera["sha256"] and segunda["status"] == "ACTIVE"


def test_el_mismo_archivo_otra_vez_es_duplicado_y_la_misma_tinta_pide_force(mundo):
    alta(mundo, label="Julio")

    _, (estado, cuerpo) = alta(mundo, label="Julio otra vez")
    assert (estado, cuerpo["error"]) == (409, "DUPLICATE_REFERENCE")
    assert cuerpo["existing"] == {"kind": "FIRMA", "refId": "julio"}

    # Otra imagen del MISMO trazo (re-codificada): misma tinta.
    _, (estado, cuerpo) = alta(mundo, label="Julio jpg", datos=recorte(formato="JPEG"))
    assert (estado, cuerpo["error"]) == (409, "SIMILAR_REFERENCE")
    _, (estado, cuerpo) = alta(mundo, label="Julio jpg", datos=recorte(formato="JPEG"), force=True)
    assert estado == 200 and cuerpo["format"] == "jpeg"
    # Otra firma de verdad entra sin más, y una etiqueta repetida no pisa a la anterior.
    reserva, (estado, _) = alta(mundo, label="Julio", datos=recorte(semilla=7))
    assert estado == 200 and reserva["refId"] == "julio-2"


def test_validaciones_de_entrada(mundo):
    def post(ruta, cuerpo):
        return llamar(mundo, "POST", references_api.BASE, ruta, cuerpo)

    assert post({"scope": "PROVEEDOR", "key": "ACME S.A."}, {"kind": "FIRMA", "label": "x"})[1]["error"] == "INVALID_REFERENCE_KEY"
    assert post({"scope": "CLIENTE", "key": "1"}, {"kind": "FIRMA", "label": "x"})[1]["error"] == "INVALID_SCOPE"
    estado, cuerpo = post(BASE, {"kind": "HOLOGRAMA", "label": "x"})
    assert (estado, cuerpo["error"], cuerpo["valid"]) == (400, "INVALID_KIND", ["FIRMA", "LOGO", "SELLO"])
    assert post(BASE, {"label": "x"})[1]["error"] == "MISSING_FIELD"
    assert post(BASE, {"kind": "FIRMA", "label": "  "})[1]["error"] == "MISSING_FIELD"
    assert llamar(mundo, "PUT", references_api.BASE, BASE)[0] == 404
    evento = {"httpMethod": "POST", "resource": references_api.BASE, "pathParameters": BASE, "body": "{no json",
              "requestContext": {"identity": {"apiKeyId": "key-ok"}}}
    assert json.loads(references_api.handle(evento, mundo.deps)["body"])["error"] == "INVALID_JSON"


def test_la_clave_llega_codificada_en_la_url(mundo):
    estado, cuerpo = llamar(mundo, "GET", references_api.BASE, {"scope": "PROVEEDOR", "key": "ACME%20S.A."})

    assert (estado, cuerpo["error"]) == (400, "INVALID_REFERENCE_KEY")


def test_hay_un_tope_de_referencias_activas_por_elemento(mundo, monkeypatch):
    monkeypatch.setattr(registro, "MAX_ACTIVE_PER_KIND", 1)
    alta(mundo, label="Julio")

    estado, cuerpo = llamar(mundo, "POST", references_api.BASE, BASE, {"kind": "FIRMA", "label": "Omar"})

    assert (estado, cuerpo["error"]) == (409, "REFERENCE_LIMIT_REACHED")


# ---------------------------------------------------------------- consulta y baja


def test_detalle_con_url_de_la_imagen_y_baja_que_la_saca_de_todo(mundo):
    reserva, _ = alta(mundo)
    ruta = {**BASE, "kind": "FIRMA", "refId": reserva["refId"]}

    estado, detalle = llamar(mundo, "GET", references_api.ONE, ruta)
    assert estado == 200 and "references/PROVEEDOR/10040/FIRMA/" in detalle["imageUrl"]

    estado, baja = llamar(mundo, "DELETE", references_api.ONE, ruta)
    assert estado == 200 and baja["status"] == "INACTIVE" and baja["deactivatedBy"] == "ERP"
    assert llamar(mundo, "GET", references_api.BASE, BASE)[1]["count"] == 0
    assert llamar(mundo, "GET", references_api.BASE, BASE, query={"includeInactive": "1"})[1]["count"] == 1
    assert fingerprint_store.list_references("PROVEEDOR", "10040", table=mundo.tabla) == {}
    assert mundo.indice.vectores == {}
    assert "imageUrl" not in llamar(mundo, "GET", references_api.ONE, ruta)[1]
    # La API solo desactiva: fila e imagen siguen ahí para la auditoría.
    assert len(mundo.tabla.items) == 1 and len(mundo.s3.objetos) == 1


def test_lo_que_no_existe_o_ya_expiro_es_404(mundo):
    ruta = {**BASE, "kind": "FIRMA", "refId": "nadie"}
    assert llamar(mundo, "GET", references_api.ONE, ruta)[0] == 404
    assert llamar(mundo, "DELETE", references_api.ONE, ruta)[0] == 404
    assert llamar(mundo, "POST", references_api.CONFIRM, ruta)[0] == 404

    # El TTL de DynamoDB borra con horas de retraso: una reserva vencida ya no existe.
    _, reserva = llamar(mundo, "POST", references_api.BASE, BASE, {"kind": "FIRMA", "label": "Julio"})
    mundo.tabla.items[("R#PROVEEDOR#10040", "FIRMA#julio")]["ExpiresAt"] = 1
    assert llamar(mundo, "POST", references_api.CONFIRM, {**BASE, "kind": "FIRMA", "refId": "julio"})[0] == 404


def test_si_el_indice_falla_la_referencia_queda_activa_y_reconfirmar_reintenta(mundo):
    mundo.indice.falla = True
    reserva, (estado, ficha) = alta(mundo)

    assert estado == 200 and ficha["status"] == "ACTIVE" and ficha["indexed"] is False
    assert mundo.tabla.items[("R#PROVEEDOR#10040", "FIRMA#lic-julio-lemus")]["IndexStatus"] == "PENDING"

    mundo.indice.falla = False
    _, otra_vez = llamar(mundo, "POST", references_api.CONFIRM, {**BASE, "kind": "FIRMA", "refId": reserva["refId"]})
    assert otra_vez["indexed"] is True and len(mundo.indice.vectores) == 1


# ---------------------------------------------------------------- CLI (alta directa) y reindexado


def test_el_alta_directa_del_cli_escribe_lo_mismo_que_la_api(mundo):
    datos = recorte()
    ficha = registro.review(datos, ELEMENTOS["FIRMA"])

    fila = mundo.reg.register("PROVEEDOR", "10021", "FIRMA", "pablo", "Pablo Ontiveros", datos, ficha,
                              actor="operador", source_file="firma1.png", for_type="CARTA_NOM35")

    assert fila["Status"] == "ACTIVE" and fila["Active"] is True and "ExpiresAt" not in fila
    assert fila["IndexStatus"] == "INDEXED" and fila["InkGrid"] == ficha["grid"]
    assert registro.find_duplicate(ficha, "FIRMA", mundo.reg.items("PROVEEDOR", "10021"))["reason"] == "SAME_FILE"
    mundo.reg.delete("PROVEEDOR", "10021", "FIRMA", "pablo")
    assert mundo.tabla.items == {} and mundo.s3.objetos == {} and mundo.indice.vectores == {}


def test_reindexar_rellena_las_referencias_registradas_antes_de_los_embeddings(mundo):
    datos = recorte()
    mundo.s3.objetos["references/PROVEEDOR/10021/FIRMA/vieja.png"] = datos
    vieja = {"Pk": "R#PROVEEDOR#10021", "Sk": "FIRMA#vieja", "Active": True,
             "S3Key": "references/PROVEEDOR/10021/FIRMA/vieja.png"}
    mundo.tabla.put_item(vieja)

    assert mundo.reg.reindex(vieja) == "INDEXED"

    fila = mundo.tabla.items[("R#PROVEEDOR#10021", "FIRMA#vieja")]
    assert fila["EmbeddingModel"] == "modelo-falso" and fila["IndexStatus"] == "INDEXED"
    assert ("PROVEEDOR", "10021", "FIRMA", "vieja") in mundo.indice.vectores
    assert mundo.reg.reindex({**vieja, "Active": False}) == "SKIPPED"
    assert mundo.reg.reindex({**vieja, "S3Key": "no-existe"}) == "FAILED"
