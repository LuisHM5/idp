"""Varias instancias del MISMO tipo dentro de un solo PDF.

`page_selector.select()` contesta "qué páginas de este documento vale la pena
pagar", que es la pregunta correcta para un pedimento: la subida trae uno (más
sus reimpresiones). Es la pregunta equivocada para una factura extranjera,
donde un PDF trae rutinariamente VARIAS facturas distintas — un archivo de dos
páginas que son dos facturas completas, o un expediente con tres de tres
proveedores. Juntarlas en un documento pierde todos los números menos uno.

`split_instances()` + `segmenter._declared_segments()` contestan la otra
pregunta: DÓNDE EMPIEZA Y TERMINA CADA UNA. Lo que se protege aquí:

- se parten las que son distintas, aunque vayan en páginas consecutivas;
- las copias del mismo documento colapsan en una;
- todo lo que el partidor no pueda contestar con confianza vuelve como UN
  segmento que cubre el archivo entero, o sea exactamente lo que pasaba antes
  de que este código existiera.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

LAMBDA_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/document-processing/resources/enrichment-lambda"
)
sys.path.insert(0, str(LAMBDA_DIR))

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests"))

from catalog_paths import active_catalog_path  # noqa: E402
from pipeline import page_selector  # noqa: E402

FACTURA_SPEC = json.loads(
    active_catalog_path(REPO_ROOT, "FACTURA_FLETE_EXTRANJERO").read_text(encoding="utf-8")
)["FACTURA_FLETE_EXTRANJERO"]["page_selection"]
FACTURA_SPEC_SIN_FLAG = {k: v for k, v in FACTURA_SPEC.items() if k != "multi_instance"}


def _page(number, text, source="textract"):
    """PageText-shaped stub — el selector solo lee number/text/source."""
    return SimpleNamespace(number=number, text=text, source=source)


# --- texto tomado de los documentos reales --------------------------------
#
# Cinco layouts distintos de los tres expedientes de ejemplo. No están
# inventados: son las frases que Textract devuelve de esas páginas, recortadas
# a lo que el selector mira.

WONDERFUL = (
    "Wonderful citrus IMPORTADOR/COMPRADOR OPERADORA FUTURAMA SA DE CV "
    "Please Remit Check Payments To: Wonderful Citrus Cooperative "
    "INVOICE Invoice Number Invoice Date Invoice Due Date {num} 07/27/2026 08/27/2026 "
    "Bill To OPERADORA FUTURAMA SA DE CV Ship To Cold Valley Storage "
    "Customer PO NO 2431668 Ship Date Order Number Sales Person Yehidy Gomez "
    "Item Description Qty Shipped Unit Price Currency Code Extended Amount "
    "Total {total} Payments 0.00 USD Balance Due {total} "
    "Please Remit USD Wire Payments To: WELLS FARGO BANK, NA"
)

BRAVANTE = (
    "Cal Packing & Storage LP dba Bravante Produce 1356 S Buttonwillow Avenue Reedley CA "
    "INVOICE Invoice #: {num} Invoice: 07/26/26 Ship: 07/26/26 Pay Terms: "
    "Sold To: Operadora Futurama S.A. De CV Ship To: Valley Cold Storage "
    "Page 1 of 1 Sale Terms: Salesperson: Garrett Steele Carrier: Currency: USD "
    "Description Quantity UOM Price Amount 1 NARANJA FRESCA 972 20,412.00 "
    "INVOICE TOTAL: 20,412.00"
)

CROSSPOINT = (
    "Crosspoint Trade Solutions, LLC 215 S Poplar St. El Paso, TX 79901 "
    "INVOICE BILL TO Operadora Futurama, SA de CV INVOICE # {num} "
    "DATE 07/27/2026 DUE DATE 08/26/2026 TERMS Net 30 "
    "DATE DESCRIPTION QTY RATE AMOUNT 07/27/2026 Freight VC-A "
    "Freight services From Valley Cold To Allende 1 375.00 375.00 "
    "BALANCE DUE $375.00 Wire Transfer: Bank: Wells Fargo Bank"
)

VALLEY_COLD = (
    "VALLEY COLD Valley Cold Storage & Transportation, LLC 101 Watson Lane Las Cruces, NM "
    "Warehouse Receipt Invoice Customer: Operadora Futurama "
    "Invoice Number: {num} Invoice Reference: SJ191703 Invoice Date: 7/27/2026 "
    "Invoice Terms: Net 30 Shipment #: 105630 "
    "Service Qty Rate Total Applying Labels 1,080.00 $0.0800 $86.40 "
    "Total Invoice $424.00"
)

# Páginas que NO son facturas extranjeras: el resto del expediente.
CFDI = (
    "EMISOR MARTHA CELIA VELOZ OÑATE RFC: VEOM500811DI9 RECEPTOR OPERADORA FUTURAMA SA DE CV "
    "Gastos y Honorarios por el Despacho Aduanero CONCEPTO IMPORTE HONORARIOS $335.00 "
    "ESTE DOCUMENTO ES UNA REPRESENTACION IMPRESA DE UN CFDI Folio Fiscal "
    "a5986465-9629-4463-a4d7-6c49fd40506e SELLO DIGITAL DEL SAT CADENA ORIGINAL "
    "TOTALES Y SALDO A PAGAR Total: 19,044.21"
)
PEDIMENTO = (
    "PEDIMENTO Página 1 de 3 NUM. PEDIMENTO: 26 07 0823 6001840 T.OPER IMP "
    "CVE. PEDIM: A1 REGIMEN: IMD CUADRO DE LIQUIDACION VALOR ADUANA "
    "DEPOSITO REFERENCIADO LINEA DE CAPTURA"
)
DODA = (
    "DOCUMENTO DE OPERACION PARA DESPACHO ADUANERO (DODA) No. DE INTEGRACION: 147768325 "
    "PATENTE O AUTORIZACION: 0823 CADENA ORIGINAL FIRMA DEL REPRESENTANTE LEGAL"
)
VUCEM = (
    "gob mx Información de Valor y de Comercialización Ventanilla digital mexicana de "
    "comercio exterior Datos del Acuse de Valor COVE2687S8N08 Datos de la mercancía"
)


def _expediente_840():
    """`260708236001840.pdf` recortado: facturas en 9, 10, 16 y copia en 25."""
    pages = {
        1: CFDI,
        5: DODA,
        6: PEDIMENTO,
        9: WONDERFUL.format(num="SJ191703", total="14,612.40"),
        10: WONDERFUL.format(num="SJ191703-1", total="9,741.60"),
        12: VUCEM,
        16: VALLEY_COLD.format(num="FP107954"),
        25: WONDERFUL.format(num="SJ191703", total="14,612.40") + " 0805.10.0065",
    }
    return [_page(n, pages.get(n, "página sin señales de este tipo")) for n in range(1, 32)]


# --- partir en instancias --------------------------------------------------

def test_two_invoices_in_a_two_page_file_are_two_documents():
    """`factura-flete-01.pdf`: dos facturas completas, una por página."""
    pages = [
        _page(1, WONDERFUL.format(num="SJ191703", total="14,612.40")),
        _page(2, WONDERFUL.format(num="SJ191703-1", total="9,741.60")),
    ]
    split = page_selector.split_instances(pages, FACTURA_SPEC)
    assert split.fail_open is False
    assert split.ranges == [(1, 1), (2, 2)]


def test_three_vendors_in_one_expediente_are_three_documents():
    pages = [
        _page(8, BRAVANTE.format(num="517157")),
        _page(12, CROSSPOINT.format(num="6331")),
        _page(13, VALLEY_COLD.format(num="FP107951")),
    ]
    split = page_selector.split_instances(pages, FACTURA_SPEC)
    assert split.ranges == [(8, 8), (12, 12), (13, 13)]


def test_repeated_copy_of_the_same_invoice_collapses():
    """El expediente imprime la factura del agente y otra copia anotada a
    mano; pagarla dos veces y luego deduplicar filas es tirar dinero."""
    split = page_selector.split_instances(_expediente_840(), FACTURA_SPEC)
    assert split.fail_open is False
    assert split.duplicates_dropped == 1
    # p25 es copia de p9 (mismo SJ191703) y desaparece; 9, 10 y 16 sobreviven.
    assert [a for a, _ in split.ranges] == [9, 10, 16]


def test_non_invoice_pages_of_the_expediente_are_never_picked_up():
    """Ni el CFDI, ni el pedimento, ni el DODA, ni el acuse VUCEM."""
    pages = [_page(1, CFDI), _page(2, PEDIMENTO), _page(3, DODA), _page(4, VUCEM)]
    scores = {
        s.number: s.score
        for s in page_selector.score_pages(pages, FACTURA_SPEC["markers"])
    }
    assert all(score < FACTURA_SPEC["min_score"] for score in scores.values()), scores


def test_a_continuation_page_stays_with_its_own_invoice():
    """`follow_pages` recoge la segunda página de una factura que no repite
    encabezado — sin invadir la factura siguiente.

    El catálogo lo deja en 0 (las facturas medidas caben en una página y
    arrastrar una de más metería el documento vecino dentro de la factura),
    así que aquí se activa explícitamente para proteger la capacidad.
    """
    spec = {**FACTURA_SPEC, "multi_instance": {"follow_pages": 1}}
    pages = [
        _page(1, CROSSPOINT.format(num="6331")),
        _page(2, "continuación de conceptos sin encabezado propio"),
        _page(3, CROSSPOINT.format(num="6332")),
    ]
    split = page_selector.split_instances(pages, spec)
    assert split.ranges == [(1, 2), (3, 3)]


def test_follow_pages_defaults_to_zero_so_a_neighbour_never_leaks_in():
    """Con el catálogo tal cual, la página siguiente a una factura (otro
    documento del expediente) NO entra en su segmento."""
    pages = [
        _page(1, CROSSPOINT.format(num="6331")),
        _page(2, CFDI),
        _page(3, VUCEM),
    ]
    split = page_selector.split_instances(pages, FACTURA_SPEC)
    assert split.ranges == [(1, 1)]


def test_page_of_label_keeps_a_multi_page_invoice_whole():
    """Con 'Page 2 of 2' la segunda página no abre una instancia falsa."""
    spec = {**FACTURA_SPEC, "duplicate_key_pattern": None}
    pages = [
        _page(1, BRAVANTE.format(num="517157").replace("Page 1 of 1", "Page 1 of 2")),
        _page(2, BRAVANTE.format(num="517157").replace("Page 1 of 1", "Page 2 of 2")),
        _page(3, CROSSPOINT.format(num="6331")),
    ]
    split = page_selector.split_instances(pages, spec)
    assert split.ranges == [(1, 2), (3, 3)]


# --- fail-open -------------------------------------------------------------

def test_a_single_invoice_alone_in_the_file_is_not_split():
    pages = [_page(1, CROSSPOINT.format(num="6331"))]
    split = page_selector.split_instances(pages, FACTURA_SPEC)
    assert split.fail_open is True
    assert split.ranges == [(1, 1)]


def test_no_boundaries_declared_means_no_splitting():
    """Sin `start_markers` ni `page_of_pattern` no hay dónde cortar."""
    spec = {k: v for k, v in FACTURA_SPEC.items() if k not in ("start_markers", "page_of_pattern")}
    pages = [
        _page(1, WONDERFUL.format(num="SJ191703", total="1.00")),
        _page(2, WONDERFUL.format(num="SJ191703-1", total="2.00")),
    ]
    split = page_selector.split_instances(pages, spec)
    assert split.fail_open is True
    assert split.ranges == [(1, 2)]


def test_nothing_clears_min_score_processes_the_whole_file():
    pages = [_page(1, CFDI), _page(2, PEDIMENTO)]
    split = page_selector.split_instances(pages, FACTURA_SPEC)
    assert split.fail_open is True
    assert split.ranges == [(1, 2)]


def test_too_many_instances_fails_open_instead_of_paying_per_page():
    """Un marcador demasiado genérico emitiría un segmento por página, y cada
    segmento es una llamada real a Bedrock."""
    spec = {**FACTURA_SPEC, "multi_instance": {"max_instances": 3}}
    pages = [_page(n, CROSSPOINT.format(num=f"600{n}")) for n in range(1, 11)]
    split = page_selector.split_instances(pages, spec)
    assert split.fail_open is True
    assert split.ranges == [(1, 10)]
    assert "max_instances" in split.reason


def test_unreadable_identity_is_never_dropped():
    """Dos páginas cuyo número no se puede leer se conservan las dos."""
    spec = {**FACTURA_SPEC, "duplicate_key_pattern": [r"NUNCA_COINCIDE_(\d+)"]}
    pages = [
        _page(1, WONDERFUL.format(num="SJ191703", total="1.00")),
        _page(2, WONDERFUL.format(num="SJ191703", total="1.00")),
    ]
    split = page_selector.split_instances(pages, spec)
    assert split.duplicates_dropped == 0
    assert split.ranges == [(1, 1), (2, 2)]


def test_invalid_duplicate_pattern_degrades_to_no_dedup():
    spec = {**FACTURA_SPEC, "duplicate_key_pattern": ["(sin cerrar"]}
    split = page_selector.split_instances(_expediente_840(), spec)
    assert split.duplicates_dropped == 0
    assert [a for a, _ in split.ranges] == [9, 10, 16, 25]


def test_empty_input():
    split = page_selector.split_instances([], FACTURA_SPEC)
    assert split.fail_open is True
    assert split.spans == []


# --- identidad -------------------------------------------------------------

def test_identity_keeps_letters_instead_of_collapsing_to_digits():
    """`SJ191703` no puede volverse `191703`: otro documento puede llevar ese
    número y colapsarlos perdería una factura."""
    texts = {1: WONDERFUL.format(num="SJ191703", total="1.00")}
    assert page_selector.identity_in(texts, [1], FACTURA_SPEC) == "SJ191703"


def test_invoice_suffix_makes_a_different_identity():
    a = {1: WONDERFUL.format(num="SJ191703", total="1.00")}
    b = {1: WONDERFUL.format(num="SJ191703-1", total="2.00")}
    assert page_selector.identity_in(a, [1], FACTURA_SPEC) != page_selector.identity_in(b, [1], FACTURA_SPEC)


def test_duplicate_key_pattern_accepts_a_bare_string_too():
    """Retrocompatible: el pedimento la declara como un solo regex."""
    catalog = json.loads(
        active_catalog_path(REPO_ROOT, "PEDIMENTO_ADUANAL").read_text(encoding="utf-8")
    )
    spec = catalog["PEDIMENTO_ADUANAL"]["page_selection"]
    assert isinstance(spec["duplicate_key_pattern"], str)
    texts = {1: "NUM. PEDIMENTO: 26 07 3435 6041353 T.OPER"}
    # Idéntico al `re.sub(r"\D", "", ...)` de antes para un identificador
    # que solo trae dígitos y espacios.
    assert page_selector.identity_in(texts, [1], spec) == "260734356041353"


# --- integración con el segmentador ---------------------------------------

def test_declared_type_without_multi_instance_stays_one_segment(monkeypatch):
    """Cualquier tipo que no declare el flag conserva el comportamiento de
    siempre: tipo declarado == el archivo entero."""
    from document_catalog import _parse_entry
    from pipeline import segmenter

    entry = _parse_entry("SIN_FLAG", {"fields": {}, "page_selection": FACTURA_SPEC_SIN_FLAG})
    monkeypatch.setattr(segmenter, "get_type_entry", lambda _t: entry)
    pages = [
        _page(1, WONDERFUL.format(num="SJ191703", total="1.00")),
        _page(2, WONDERFUL.format(num="SJ191703-1", total="2.00")),
    ]
    segments, usage = segmenter.segment_pages(pages, declared_type="SIN_FLAG")
    assert usage == {}
    assert [(s.page_start, s.page_end) for s in segments] == [(1, 2)]


def test_declared_type_with_multi_instance_yields_one_segment_per_invoice(monkeypatch):
    import document_catalog
    from document_catalog import _parse_entry
    from pipeline import segmenter

    entry = _parse_entry(
        "FACTURA_FLETE_EXTRANJERO",
        {
            "fields": {},
            "occurrence_mode": "multiple",
            "page_selection": FACTURA_SPEC,
        },
    )
    monkeypatch.setattr(segmenter, "get_type_entry", lambda _t: entry)
    monkeypatch.setattr(document_catalog, "get_type_entry", lambda _t: entry)
    segments, usage = segmenter.segment_pages(
        _expediente_840(), declared_type="FACTURA_FLETE_EXTRANJERO",
    )
    # Sin llamada a Bedrock: el partidor es puro regex sobre texto ya extraído.
    assert usage == {}
    assert [(s.page_start, s.page_end) for s in segments] == [
        (9, 9),
        (10, 10),
        (16, 16),
        (25, 25),
    ]
    assert all(s.doc_type == "FACTURA_FLETE_EXTRANJERO" for s in segments)


def test_kill_switch_puts_a_declared_type_back_on_one_segment(monkeypatch):
    from document_catalog import _parse_entry
    from pipeline import segmenter

    entry = _parse_entry("FACTURA_FLETE_EXTRANJERO", {"fields": {}, "page_selection": FACTURA_SPEC})
    monkeypatch.setattr(segmenter, "get_type_entry", lambda _t: entry)
    monkeypatch.setenv("PAGE_SELECTION_ENABLED", "false")
    segments, _ = segmenter.segment_pages(
        _expediente_840(), declared_type="FACTURA_FLETE_EXTRANJERO"
    )
    assert [(s.page_start, s.page_end) for s in segments] == [(1, 31)]



# --- regresión contra el OCR REAL -----------------------------------------
#
# Los tests de arriba usan texto escrito a mano: dicen que la LÓGICA parte
# bien, no que los marcadores del catálogo estén calibrados. Esto último solo
# lo puede decir el texto que Textract devuelve de los documentos reales, así
# que `fixtures/facturas_extranjeras_ocr.json` lo guarda — recortado a 1100
# caracteres por página y cortado antes del bloque bancario, que no tiene por
# qué vivir en el repo.
#
# Regenerarlo cuando cambien los documentos de ejemplo:
#   python scripts/analizar-seleccion-paginas.py --type FACTURA_FLETE_EXTRANJERO \
#       --dir <carpeta> --ocr --cache <dir>
# y volver a recortar el caché igual que la primera vez.

OCR_REAL = json.loads(
    (Path(__file__).parent / "fixtures" / "facturas_extranjeras_ocr.json").read_text(encoding="utf-8")
)


def _ocr(nombre):
    return [_page(p["number"], p["text"]) for p in OCR_REAL[nombre]]


def test_ocr_real_expediente_840():
    """31 páginas: tres facturas (Wonderful x2 + Valley Cold) y una copia
    anotada a mano de la primera, que colapsa."""
    split = page_selector.split_instances(_ocr("260708236001840"), FACTURA_SPEC)
    assert split.fail_open is False
    assert split.ranges == [(9, 9), (10, 10), (16, 16)]
    assert split.duplicates_dropped == 1  # la copia de la pág 25


def test_ocr_real_expediente_841():
    """Tres proveedores distintos: mercancía (Bravante), flete (Crosspoint) y
    almacenaje (Valley Cold), más la copia anotada de la primera."""
    split = page_selector.split_instances(_ocr("260708236001841"), FACTURA_SPEC)
    assert split.ranges == [(8, 8), (12, 12), (13, 13)]
    assert split.duplicates_dropped == 1  # la copia de la pág 22


def test_ocr_real_dos_facturas_en_dos_paginas():
    split = page_selector.split_instances(_ocr("factura-flete-01"), FACTURA_SPEC)
    assert split.ranges == [(1, 1), (2, 2)]


def test_ocr_real_el_packing_list_no_es_una_factura():
    """El margen que sostiene min_score=4: el packing list repite 'INVOICE
    NUMBER' y 'SHIPPER' y llega a 3; la factura real más floja llega a 5."""
    scores = {
        s.number: s.score
        for s in page_selector.score_pages(_ocr("260708236001840"), FACTURA_SPEC["markers"])
    }
    facturas = [scores[n] for n in (9, 10, 16, 25)]
    packing_list = scores[18]
    assert min(facturas) >= FACTURA_SPEC["min_score"]
    assert packing_list < FACTURA_SPEC["min_score"]


def test_ocr_real_ninguna_pagina_de_otro_tipo_alcanza_el_umbral():
    """CFDI, pedimento, DODA, COVE, manifestación de valor, certificados
    fitosanitarios, carta porte: nada de eso puede puntuar como factura."""
    facturas = {"260708236001840": {9, 10, 16, 25}, "260708236001841": {8, 12, 13, 22}}
    for nombre, esperadas in facturas.items():
        scores = {
            s.number: s.score
            for s in page_selector.score_pages(_ocr(nombre), FACTURA_SPEC["markers"])
        }
        colados = {
            n: score for n, score in scores.items()
            if n not in esperadas and score >= FACTURA_SPEC["min_score"]
        }
        assert not colados, f"{nombre}: páginas que no son facturas alcanzaron el umbral: {colados}"


def test_ocr_real_identidades_leidas_de_cada_factura():
    """Las cuatro etiquetas distintas ('Invoice Number' en tabla, 'Invoice #:',
    'INVOICE #' e 'Invoice Number:') tienen que dar el número, no la palabra
    'Invoice' ni la fecha de al lado."""
    textos = {p["number"]: p["text"] for p in OCR_REAL["260708236001841"]}
    ident = lambda t, n: page_selector.identity_in(t, [n], FACTURA_SPEC)
    assert ident(textos, 8) == "517157"    # Bravante
    assert ident(textos, 12) == "6331"     # Crosspoint
    # OJO: el número impreso es FP107951; Textract lee un 6 donde va un 5 sobre
    # este escaneo. Se fija el valor REAL que devuelve el OCR, no el del papel:
    # para deduplicar copias basta con que la identidad sea estable, y fingir
    # aquí que el OCR acierta escondería un problema de lectura que sí afecta a
    # NUMERO_FACTURA. Si algún día mejora, este test lo dirá.
    assert ident(textos, 13) == "FP107961"  # Valley Cold

    textos_840 = {p["number"]: p["text"] for p in OCR_REAL["260708236001840"]}
    assert ident(textos_840, 9) == "SJ191703"
    assert ident(textos_840, 10) == "SJ1917031"


# --- arrays que el modelo devuelve como string -----------------------------
#
# Medido en la corrida real de `factura-flete-01.pdf`: el MISMO modelo, en la
# MISMA llamada, devolvió CONCEPTOS como array de verdad para el segmento de
# la página 1 y como string JSON (`"[{...}]"`) para el de la página 2, pese al
# tool schema que declara `"type": "array"`. El string se descartaba en
# silencio y esa factura salía con cero conceptos, incumpliendo su propia
# regla `minimum_items`.

def test_stringified_array_is_parsed_back():
    from pipeline.text_extractor import _coerce_container

    assert _coerce_container('[{"DESCRIPCION": "Freight"}]', list) == [{"DESCRIPCION": "Freight"}]


def test_stringified_object_is_parsed_back():
    from pipeline.text_extractor import _coerce_container

    assert _coerce_container('{"BANCO": "Wells Fargo"}', dict) == {"BANCO": "Wells Fargo"}


def test_a_real_container_is_returned_untouched():
    from pipeline.text_extractor import _coerce_container

    rows = [{"DESCRIPCION": "Freight"}]
    assert _coerce_container(rows, list) is rows


def test_a_string_that_is_not_the_expected_container_is_left_alone():
    """Nada se adivina: un escalar sigue siendo un escalar, y un objeto donde
    se esperaba una lista lo rechaza el `isinstance` de siempre."""
    from pipeline.text_extractor import _coerce_container

    assert _coerce_container("Freight services", list) == "Freight services"
    assert _coerce_container('{"A": 1}', list) == '{"A": 1}'
    assert _coerce_container("", list) == ""
    assert _coerce_container(None, list) is None


def test_stringified_rows_survive_the_field_tree_parser():
    """De extremo a extremo del parser, no solo del helper."""
    from document_catalog import _parse_entry
    from pipeline.text_extractor import _parse_field_tree_response

    entry = _parse_entry("CON_ARRAY", {
        "fields": {
            "CONCEPTOS": {
                "type": "array",
                "item_fields": {"DESCRIPCION": {"type": "string"}, "IMPORTE": {"type": "number"}},
            }
        }
    })
    raw = {"CONCEPTOS": '[{"DESCRIPCION": "Freight VC-A", "IMPORTE": 375.0}]'}
    entity, _, _ = _parse_field_tree_response(raw, entry.field_tree, 0.9)
    assert entity["CONCEPTOS"] == [{"DESCRIPCION": "Freight VC-A", "IMPORTE": 375.0}]


def test_a_leaked_next_argument_does_not_poison_the_array():
    """El fallo real medido: el string traía el array Y una fuga del argumento
    siguiente. `json.loads` sobre todo eso revienta; `raw_decode` se queda con
    el valor del principio y tira la fuga."""
    from pipeline.text_extractor import _coerce_container

    corrupto = (
        '[\n  {"DESCRIPCION": "CA Valencia Standard", "IMPORTE": 9741.60}\n],\n'
        '<parameter name="MONEDA">{"value": "USD", "confidence": 1.0}'
    )
    assert _coerce_container(corrupto, list) == [
        {"DESCRIPCION": "CA Valencia Standard", "IMPORTE": 9741.60}
    ]


# --- copias repetidas en la ruta SIN tipo declarado ------------------------
#
# Al clasificador se le dice que una reimpresión es un documento aparte, que es
# la instrucción correcta: desde el texto de una página no puede saber si dos
# páginas parecidas son un documento o dos. Decidir cuáles son EL MISMO
# documento sí es determinista, y el tipo ya declaró dónde leer su identidad.

def _segmentos_declarados(monkeypatch, entry):
    from pipeline import segmenter

    monkeypatch.setattr(
        segmenter, "get_type_entry",
        lambda t: entry if t == "FACTURA_FLETE_EXTRANJERO" else _otro_entry(),
    )
    return segmenter


def _otro_entry():
    from document_catalog import _parse_entry

    return _parse_entry("OTRO_TIPO", {"fields": {}})


def test_classifier_path_marks_a_repeated_invoice_as_a_copy(monkeypatch):
    """El expediente 841 real: la factura 517157 de la pág 8 se reimprime
    anotada a mano en la 22, y el clasificador devuelve las dos."""
    from document_catalog import _parse_entry
    from pipeline import segmenter
    from pipeline.segmenter import Segment, _collapse_duplicate_segments

    entry = _parse_entry("FACTURA_FLETE_EXTRANJERO", {"fields": {}, "page_selection": FACTURA_SPEC})
    _segmentos_declarados(monkeypatch, entry)

    pages = _ocr("260708236001841")
    segmentos = [
        Segment("FACTURA_FLETE_EXTRANJERO", 8, 8, 0.95),
        Segment("FACTURA_FLETE_EXTRANJERO", 12, 12, 0.95),
        Segment("FACTURA_FLETE_EXTRANJERO", 13, 13, 0.95),
        Segment("FACTURA_FLETE_EXTRANJERO", 22, 22, 0.9),
    ]
    resultado = _collapse_duplicate_segments(segmentos, pages)

    # Nada desaparece: la cobertura de páginas queda intacta.
    assert [(s.page_start, s.page_end) for s in resultado] == [(8, 8), (12, 12), (13, 13), (22, 22)]
    facturas = [s for s in resultado if s.doc_type == "FACTURA_FLETE_EXTRANJERO"]
    assert [s.page_start for s in facturas] == [8, 12, 13]
    copia = next(s for s in resultado if s.doc_type == "OTRO")
    assert copia.page_start == 22
    assert copia.subtype == "COPIA_DUPLICADA"


def test_classifier_path_multiple_preserva_la_copia_fisica(monkeypatch):
    from document_catalog import _parse_entry
    from pipeline.segmenter import Segment, _collapse_duplicate_segments

    entry = _parse_entry(
        "FACTURA_FLETE_EXTRANJERO",
        {
            "fields": {},
            "page_selection": FACTURA_SPEC,
        },
    )
    monkeypatch.setattr("pipeline.segmenter.get_type_entry", lambda _t: entry)
    pages = [
        _page(1, WONDERFUL.format(num="SJ191703", total="14,612.40")),
        _page(2, WONDERFUL.format(num="SJ191703", total="14,612.40")),
    ]
    resultado = _collapse_duplicate_segments(
        [
            Segment("FACTURA_FLETE_EXTRANJERO", 1, 1, 0.9),
            Segment("FACTURA_FLETE_EXTRANJERO", 2, 2, 0.9),
        ],
        pages,
        {"FACTURA_FLETE_EXTRANJERO": "all"},
    )
    assert [segment.doc_type for segment in resultado] == [
        "FACTURA_FLETE_EXTRANJERO",
        "FACTURA_FLETE_EXTRANJERO",
    ]


def test_expediente_840_corrige_el_rango_9_10_y_devuelve_cuatro(monkeypatch):
    """Regresión del invoices-flete.pdf: el modelo agrupó 9-10, dejó 16 aparte
    y detectó 25 como copia. `multiple` debe publicar cuatro ocurrencias."""
    from document_catalog import _parse_entry
    from pipeline import segmenter
    from pipeline.segmenter import Segment

    entry = _parse_entry(
        "FACTURA_FLETE_EXTRANJERO",
        {
            "fields": {},
            "page_selection": FACTURA_SPEC,
        },
    )
    monkeypatch.setattr(segmenter, "get_type_entry", lambda _t: entry)
    segmentos = [
        Segment("FACTURA_FLETE_EXTRANJERO", 9, 10, 0.94),
        Segment("FACTURA_FLETE_EXTRANJERO", 16, 16, 0.90),
        Segment("FACTURA_FLETE_EXTRANJERO", 25, 25, 0.88),
    ]

    resultado = segmenter._split_typed_instances(
        segmentos, _ocr("260708236001840"),
        {"FACTURA_FLETE_EXTRANJERO": "all"},
    )

    assert [(s.page_start, s.page_end) for s in resultado] == [
        (9, 9),
        (10, 10),
        (16, 16),
        (25, 25),
    ]


def test_classifier_path_leaves_different_invoices_alone(monkeypatch):
    from document_catalog import _parse_entry
    from pipeline.segmenter import Segment, _collapse_duplicate_segments

    entry = _parse_entry("FACTURA_FLETE_EXTRANJERO", {"fields": {}, "page_selection": FACTURA_SPEC})
    _segmentos_declarados(monkeypatch, entry)

    pages = _ocr("260708236001840")
    segmentos = [
        Segment("FACTURA_FLETE_EXTRANJERO", 9, 9, 0.95),
        Segment("FACTURA_FLETE_EXTRANJERO", 10, 10, 0.95),
    ]
    resultado = _collapse_duplicate_segments(segmentos, pages)
    assert all(s.doc_type == "FACTURA_FLETE_EXTRANJERO" for s in resultado)


def test_classifier_path_never_touches_a_type_without_the_flag(monkeypatch):
    """Un pedimento imprime la MISMA identidad en sus copias a propósito y
    tiene su propio mecanismo; nada aquí puede tocarlo."""
    from pipeline import segmenter
    from pipeline.segmenter import Segment, _collapse_duplicate_segments

    monkeypatch.setattr(segmenter, "get_type_entry", lambda _t: _otro_entry())
    segmentos = [
        Segment("CUALQUIER_TIPO", 1, 3, 0.9),
        Segment("CUALQUIER_TIPO", 4, 6, 0.9),
    ]
    resultado = _collapse_duplicate_segments(segmentos, _ocr("260708236001841"))
    assert resultado == segmentos


def test_classifier_path_keeps_the_longest_copy(monkeypatch):
    """Igual que el selector: la copia más larga gana, porque un resumen de
    una página puede venir antes que el documento completo."""
    from document_catalog import _parse_entry
    from pipeline.segmenter import Segment, _collapse_duplicate_segments

    entry = _parse_entry("FACTURA_FLETE_EXTRANJERO", {"fields": {}, "page_selection": FACTURA_SPEC})
    _segmentos_declarados(monkeypatch, entry)

    pages = _ocr("260708236001841")
    segmentos = [
        Segment("FACTURA_FLETE_EXTRANJERO", 22, 22, 0.9),   # copia suelta, 1 pág
        Segment("FACTURA_FLETE_EXTRANJERO", 7, 8, 0.95),    # el original, 2 págs
    ]
    resultado = _collapse_duplicate_segments(segmentos, pages)
    ganador = next(s for s in resultado if s.doc_type == "FACTURA_FLETE_EXTRANJERO")
    assert (ganador.page_start, ganador.page_end) == (7, 8)
