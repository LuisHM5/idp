#!/usr/bin/env python3
"""Calibra la etapa de autenticidad e integridad SIN gastar un centavo.

Corre sobre una carpeta (o un archivo) las capas DETERMINISTAS de
`pipeline/authenticity.py` — integridad del archivo, estructura del PDF y
estadística de pixeles — con la política que el tipo declara en su catálogo,
y reporta por archivo el veredicto y los motivos. Sin Bedrock, sin Textract,
sin DynamoDB, sin credenciales AWS: las capas de visión, memoria y referencias
se apagan aquí a propósito (son las que cuestan o las que escriben).

La capa de integridad (¿el archivo fue tocado después de generarse?) corre
SIEMPRE aquí, pida o no el tipo sus señales, porque es lo que hay que medir
antes de agregarle a un tipo `"$presets": ["INTEGRIDAD_PDF"]`: cuántos
archivos reales traen actualizaciones incrementales, anotaciones o texto sobre
el escaneo. `--sin-integridad` la apaga para ver solo lo que el tipo declara.

Para qué sirve:
  - Antes de activar la verificación en un tipo nuevo, o antes de cambiar un
    `on_fail` de "review" a "reject": ver cuántos documentos REALES caerían.
  - Ajustar umbrales (`structure.*`, `pixels.*`, `integrity.*`) con datos y no a ojo.
  - Entender por qué un documento salió DIGITAL, INDETERMINADO o con un
    motivo de integridad (`--detalle` imprime la cadena de guardados, fechas,
    software, firmas, anotaciones y texto sobre imagen).

Uso:
    pnpm autenticidad -- --dir test-docs/nom35
    pnpm autenticidad -- --file test-docs/nom35/samples/10037/doc1.pdf --detalle
    pnpm autenticidad -- --dir facturas/ --type FACTURA_HONORARIOS --json salida.json

`--type` por defecto es CARTA_NOM35; cualquier tipo sirve, declare o no un
bloque `authenticity` (sin bloque corre con los defaults). Lee el JSON activo
bajo `catalogs/` directamente, así que NO necesita `pnpm build` ni refleja lo
que haya en DynamoDB.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"))

EXTENSIONES = {".pdf", ".jpg", ".jpeg", ".png"}


def _catalogo_activo(type_id: str) -> Path:
    candidatos = [
        p for p in (REPO / "catalogs").rglob(f"{type_id}.json")
        if "save" not in p.relative_to(REPO / "catalogs").parts
    ]
    if len(candidatos) != 1:
        raise SystemExit(
            f"Se esperaba exactamente un catálogo activo para {type_id}, hay {len(candidatos)}: {candidatos}"
        )
    return candidatos[0]


CAMPO_CALIBRACION = "_CAL_"


def _cargar_presets() -> None:
    """`$presets` se resuelve desde `_shared.json`, que en la Lambda viaja en el
    bundle; aquí se lee directo del repo para que un tipo con presets se parsee
    igual que desplegado."""
    import document_catalog

    compartido = REPO / "catalogs" / "_shared.json"
    if compartido.is_file():
        datos = json.loads(compartido.read_text(encoding="utf-8"))
        document_catalog._FIELD_PRESETS.update(
            {k: v for k, v in (datos.get("_field_presets") or {}).items() if not k.startswith("__")})
        document_catalog._COMMON_ITEM_FIELDS.update(
            {k: v for k, v in (datos.get("_common_item_fields") or {}).items() if not k.startswith("__")})


def _entry_sin_capas_de_pago(type_id: str, con_integridad: bool = True):
    from document_catalog import _parse_entry
    from pipeline import pdf_integrity

    _cargar_presets()
    crudo = json.loads(_catalogo_activo(type_id).read_text(encoding="utf-8"))[type_id]
    crudo = copy.deepcopy(crudo)
    bloque = crudo.get("authenticity")
    if not isinstance(bloque, dict):
        # Sin bloque el tipo corre con los defaults (es lo mismo que hace la Lambda).
        bloque = crudo["authenticity"] = {}
    # Las tres capas que cuestan o escriben, apagadas: lo que queda es gratis.
    bloque["vision"] = False
    bloque["memory"] = {"enabled": False}
    bloque["references"] = {"enabled": False}
    if con_integridad:
        # Campos de calibración: uno por señal de integridad (y el ELA), sin
        # regla, para que el orquestador bajo demanda corra esas capas aunque
        # el tipo todavía no las declare. Se quitan de la salida (`_CAL_*`).
        campos = crudo.setdefault("fields", {})
        for señal in list(pdf_integrity.SIGNALS) + ["pixels.error_level_uniform"]:
            nombre = CAMPO_CALIBRACION + señal.upper().replace(".", "_")
            if not any(isinstance(f, dict) and f.get("authenticity_source") == señal for f in campos.values()):
                campos[nombre] = {"type": "string", "required": False, "computed": True,
                                  "authenticity_source": señal, "hint": "calibración", "validation": []}
    return _parse_entry(type_id, crudo)


def _archivos(args: argparse.Namespace) -> List[Path]:
    if args.file:
        return [Path(args.file)]
    raiz = Path(args.dir)
    if not raiz.is_dir():
        raise SystemExit(f"No existe la carpeta: {raiz}")
    excluir = re.compile(args.excluir, re.IGNORECASE) if args.excluir else None
    return sorted(
        p for p in raiz.rglob("*")
        if p.is_file() and p.suffix.lower() in EXTENSIONES and not (excluir and excluir.search(p.name))
    )


def analizar(path: Path, entry: Any, type_id: str) -> Dict[str, Any]:
    from pipeline import authenticity

    datos = path.read_bytes()
    es_imagen = path.suffix.lower() in (".jpg", ".jpeg", ".png")
    reader = None
    paginas = 1
    if not es_imagen:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(datos))
        paginas = len(reader.pages)
    doc: Dict[str, Any] = {"documentType": type_id, "pageStart": 1, "pageEnd": paginas}
    authenticity.run(doc, entry, authenticity.source_for(datos, reader, es_imagen), {})
    campos = {k: v for k, v in (doc.get("_entities") or {}).items() if not k.startswith(CAMPO_CALIBRACION)}
    return {"archivo": str(path), "paginas": paginas, "campos": campos,
            "auditoria": doc.get("authenticity") or {}}


_NO_LISTAR = {"PAGINAS_SIN_ANALIZAR", "FIRMA_DIGITAL_PRESENTE"}


def _fila(resultado: Dict[str, Any], raiz: Optional[Path]) -> str:
    auditoria = resultado["auditoria"]
    estructura = auditoria.get("structure") or {}
    pixeles = auditoria.get("pixels") or {}
    nombre = resultado["archivo"]
    if raiz is not None:
        try:
            nombre = str(Path(nombre).relative_to(raiz))
        except ValueError:
            pass
    fondo = pixeles.get("naturalBackground")
    fondo_txt = "bitonal" if pixeles.get("bilevel") else {True: "natural", False: "ARTIFICIAL", None: "-"}[fondo]
    motivos = ",".join(r["code"] for r in auditoria.get("reasons") or [] if r["code"] not in _NO_LISTAR)
    return (f"{nombre[:38]:38s} {estructura.get('verdict', 'NO_APLICA'):16s} "
            f"{estructura.get('origin', 'IMAGEN'):11s} {fondo_txt:10s} {_integridad_txt(auditoria):11s} {motivos}")


def _integridad_txt(auditoria: Dict[str, Any]) -> str:
    """Columna `integridad`: estado de la capa + generaciones del archivo."""
    estado = (auditoria.get("layers") or {}).get("integrity")
    if estado in (None, "NO_SOLICITADA", "DESACTIVADA"):
        return "-"
    archivo = (auditoria.get("integrity") or {}).get("file") or {}
    generaciones = (archivo.get("chain") or {}).get("generations")
    etiqueta = {"OK": "ok", "FALLA": "HALLAZGO", "ERROR": "error", "NO_APLICA": "n/a"}.get(estado, estado)
    return f"{etiqueta} g={generaciones}" if generaciones is not None else etiqueta


def _detalle(resultado: Dict[str, Any]) -> None:
    auditoria = resultado["auditoria"]
    estructura = auditoria.get("structure") or {}
    meta = estructura.get("metadata") or {}
    if meta:
        print("    metadatos:", "; ".join(f"{k}={v}" for k, v in meta.items()))
    for pagina in estructura.get("perPage") or []:
        print(f"    pág {pagina['page']}: {pagina['verdict']}  texto visible={pagina['visibleTextChars']} "
              f"invisible={pagina['invisibleTextChars']}  fuentes={len(pagina.get('fonts') or [])}")
        for imagen in pagina.get("images") or []:
            print(f"        imagen {imagen['width']}x{imagen['height']} cobertura={imagen['coverage']:.2%} "
                  f"ppi={imagen['ppi']} filtros={','.join(imagen['filters']) or '-'} "
                  f"transparencia={'sí' if imagen['transparency'] else 'no'} sha256={str(imagen['sha256'])[:12]}"
                  + ("  [anotación]" if imagen.get("fromAnnotation") else ""))
    pixeles = auditoria.get("pixels") or {}
    for pagina, stats in (pixeles.get("perPage") or {}).items():
        print(f"    pixeles pág {pagina}: {stats}")
    for pagina, stats in ((pixeles.get("errorLevel") or {}).get("perPage") or {}).items():
        print(f"    ELA pág {pagina}: {stats}")
    _detalle_integridad(auditoria.get("integrity") or {})


def _detalle_integridad(integridad: Dict[str, Any]) -> None:
    archivo = integridad.get("file") or {}
    if archivo:
        cadena = archivo.get("chain") or {}
        print(f"    integridad: generaciones={cadena.get('generations')} startxref={cadena.get('startxrefCount')} "
              f"%%EOF={cadena.get('eofCount')} linearizado={'sí' if cadena.get('linearized') else 'no'}"
              + (f" (/L coincide={cadena.get('linearizedLengthMatches')})" if cadena.get("linearized") else "")
              + (f" híbrido={cadena.get('hybridXrefStm')}" if cadena.get("hybridXrefStm") else "")
              + (f" error={cadena.get('error')}" if cadena.get("error") else ""))
        fechas = archivo.get("dates") or {}
        print(f"        fechas: creación={fechas.get('creation')} modificación={fechas.get('modification')} "
              f"xmp={fechas.get('xmpCreate')}/{fechas.get('xmpModify')} consistentes={fechas.get('consistent')}"
              + (f" ({'; '.join(fechas.get('details') or [])})" if fechas.get("details") else ""))
        software = archivo.get("software") or {}
        print(f"        software: producer={software.get('producer')} creator={software.get('creator')} "
              f"xmp={software.get('xmpCreatorTool')} historial={software.get('historyAgents')} "
              f"editor={software.get('editor')}")
        firmas = archivo.get("signatures") or {}
        if firmas.get("signed"):
            print(f"        firmas digitales: {firmas.get('count')} intacta={firmas.get('intact')} "
                  f"{[i.get('signer') or i.get('name') for i in firmas.get('items') or []]}")
        formularios = archivo.get("forms") or {}
        if formularios.get("fields"):
            print(f"        formulario: {formularios.get('fields')} campos, {formularios.get('filled')} con valor, "
                  f"tipos={formularios.get('kinds')}")
        activo = archivo.get("activeContent") or {}
        if activo.get("kinds") or activo.get("embeddedFiles"):
            print(f"        contenido activo: {activo.get('kinds')} adjuntos={activo.get('embeddedFiles')}")
    for pagina in integridad.get("perPage") or []:
        partes = []
        if pagina.get("annotationSubtypes"):
            partes.append(f"anotaciones={pagina['annotationSubtypes']}")
        if pagina.get("textAfterFullPageImageChars") is not None:
            partes.append(f"texto sobre escaneo={pagina['textAfterFullPageImageChars']} chars")
        if pagina.get("imagesOverText"):
            partes.append(f"imágenes sobre texto={len(pagina['imagesOverText'])}")
        if pagina.get("actions"):
            partes.append(f"acciones={pagina['actions']}")
        if partes:
            print(f"        pág {pagina.get('page')}: " + "  ".join(partes))
    imagen = integridad.get("image") or {}
    if imagen:
        print(f"    imagen: formato={imagen.get('format')} software={imagen.get('software')} "
              f"app13={imagen.get('photoshopApp13')} xmp={imagen.get('xmpAgents')} exif={imagen.get('exif')} "
              f"editor={imagen.get('editor')} cámara={imagen.get('cameraMetadata')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default="test-docs/nom35", help="Carpeta a recorrer (recursivo).")
    parser.add_argument("--file", help="Un solo archivo (gana sobre --dir).")
    parser.add_argument("--type", default="CARTA_NOM35", help="Tipo cuyo bloque `authenticity` se usa.")
    parser.add_argument(
        "--excluir", default=r"^(firma|sello|logo)[\w-]*\.(png|jpe?g)$",
        help="Regex de nombres de archivo a ignorar. Por defecto, los recortes de referencia "
             "(firma*/sello*/logo*) que conviven con las cartas en test-docs/nom35. '' para no excluir nada.",
    )
    parser.add_argument("--detalle", action="store_true", help="Imprime páginas, imágenes y estadísticas.")
    parser.add_argument("--sin-integridad", action="store_true",
                        help="No forzar la capa de integridad: solo lo que el tipo declara.")
    parser.add_argument("--json", help="Escribe el resultado completo a este archivo.")
    # pnpm entrega el `--` separador tal cual (`pnpm x -- --flag`); argparse lo
    # leería como "fin de opciones" y rechazaría todo lo que sigue.
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    # Defensa en profundidad: aunque las capas de pago ya van apagadas en el
    # entry, sin tabla configurada la memoria tampoco podría escribir nada.
    os.environ.pop("FINGERPRINT_TABLE", None)

    entry = _entry_sin_capas_de_pago(args.type, con_integridad=not args.sin_integridad)
    archivos = _archivos(args)
    if not archivos:
        print("No hay archivos .pdf/.jpg/.jpeg/.png que analizar.")
        return 1

    raiz = None if args.file else Path(args.dir)
    print(f"{'archivo':38s} {'estructura':16s} {'origen':11s} {'fondo':10s} {'integridad':11s} motivos")
    print("-" * 122)
    resultados, conteo, conteo_integridad = [], {}, {}
    for path in archivos:
        try:
            resultado = analizar(path, entry, args.type)
        except Exception as e:  # un archivo ilegible no detiene la corrida
            print(f"{str(path)[:38]:38s} ERROR            {e}")
            continue
        resultados.append(resultado)
        veredicto = (resultado["auditoria"].get("structure") or {}).get("verdict", "NO_APLICA")
        conteo[veredicto] = conteo.get(veredicto, 0) + 1
        for motivo in resultado["auditoria"].get("reasons") or []:
            if motivo.get("layer") in ("integrity", "pixels") and motivo["code"] not in _NO_LISTAR:
                conteo_integridad[motivo["code"]] = conteo_integridad.get(motivo["code"], 0) + 1
        print(_fila(resultado, raiz))
        if args.detalle:
            _detalle(resultado)

    print("-" * 122)
    total = len(resultados)
    print(f"{total} archivo(s): " + ", ".join(f"{k}={v}" for k, v in sorted(conteo.items())))
    digitales = conteo.get("DIGITAL", 0)
    if total:
        print(f"Con `CAPTURA_FISICA` en on_fail=\"reject\" se rechazarían {digitales} de {total} "
              f"({digitales / total:.0%}); en \"review\" van a revisión.")
    if total and not args.sin_integridad:
        if conteo_integridad:
            print("Hallazgos de integridad (cuántos archivos caerían por cada señal si el tipo la declarara):")
            for codigo, n in sorted(conteo_integridad.items(), key=lambda kv: -kv[1]):
                print(f"    {codigo:45s} {n:4d} de {total} ({n / total:.0%})")
        else:
            print("Hallazgos de integridad: ninguno en estos archivos.")
    if args.json:
        Path(args.json).write_text(json.dumps(resultados, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"Resultado completo en {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
