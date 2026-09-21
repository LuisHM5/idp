#!/usr/bin/env python3
"""Calibra la etapa de autenticidad física SIN gastar un centavo.

Corre sobre una carpeta (o un archivo) las dos capas DETERMINISTAS de
`pipeline/authenticity.py` — estructura del PDF y estadística de pixeles — con
la política que el tipo declara en su catálogo, y reporta por archivo el
veredicto y los motivos. Sin Bedrock, sin Textract, sin DynamoDB, sin
credenciales AWS: las capas de visión, memoria y referencias se apagan aquí a
propósito (son las que cuestan o las que escriben).

Para qué sirve:
  - Antes de activar `authenticity` en un tipo nuevo, o antes de cambiar un
    `on_fail` de "review" a "reject": ver cuántos documentos REALES caerían.
  - Ajustar umbrales (`structure.*`, `pixels.*`) con datos y no a ojo.
  - Entender por qué un documento salió DIGITAL o INDETERMINADO.

Uso:
    pnpm autenticidad -- --dir test-docs/nom35
    pnpm autenticidad -- --file test-docs/nom35/samples/10037/doc1.pdf --detalle
    pnpm autenticidad -- --dir test-docs/nom35 --type CARTA_NOM35 --json salida.json

`--type` por defecto es CARTA_NOM35; cualquier tipo que declare `authenticity`
sirve. Lee el JSON activo bajo `catalogs/` directamente, así que NO necesita
`pnpm build` ni refleja lo que haya en DynamoDB.
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


def _entry_sin_capas_de_pago(type_id: str):
    from document_catalog import _parse_entry

    crudo = json.loads(_catalogo_activo(type_id).read_text(encoding="utf-8"))[type_id]
    bloque = crudo.get("authenticity")
    if not isinstance(bloque, dict):
        raise SystemExit(f"{type_id} no declara un bloque `authenticity` en su catálogo.")
    crudo = copy.deepcopy(crudo)
    # Las tres capas que cuestan o escriben, apagadas: lo que queda es gratis.
    crudo["authenticity"]["vision"] = False
    crudo["authenticity"]["memory"] = {"enabled": False}
    crudo["authenticity"]["references"] = {"enabled": False}
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
    return {"archivo": str(path), "paginas": paginas, "campos": doc.get("_entities") or {},
            "auditoria": doc.get("authenticity") or {}}


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
    motivos = ",".join(r["code"] for r in auditoria.get("reasons") or [] if r["code"] != "PAGINAS_SIN_ANALIZAR")
    return (f"{nombre[:38]:38s} {estructura.get('verdict', 'NO_APLICA'):16s} "
            f"{estructura.get('origin', 'IMAGEN'):11s} {fondo_txt:10s} {motivos}")


def _detalle(resultado: Dict[str, Any]) -> None:
    estructura = resultado["auditoria"].get("structure") or {}
    meta = estructura.get("metadata") or {}
    if meta:
        print("    metadatos:", "; ".join(f"{k}={v}" for k, v in meta.items()))
    for pagina in estructura.get("perPage") or []:
        print(f"    pág {pagina['page']}: {pagina['verdict']}  texto visible={pagina['visibleTextChars']} "
              f"invisible={pagina['invisibleTextChars']}  fuentes={len(pagina.get('fonts') or [])}")
        for imagen in pagina.get("images") or []:
            print(f"        imagen {imagen['width']}x{imagen['height']} cobertura={imagen['coverage']:.2%} "
                  f"ppi={imagen['ppi']} filtros={','.join(imagen['filters']) or '-'} "
                  f"transparencia={'sí' if imagen['transparency'] else 'no'} sha256={str(imagen['sha256'])[:12]}")
    for pagina, stats in ((resultado["auditoria"].get("pixels") or {}).get("perPage") or {}).items():
        print(f"    pixeles pág {pagina}: {stats}")


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
    parser.add_argument("--json", help="Escribe el resultado completo a este archivo.")
    # pnpm entrega el `--` separador tal cual (`pnpm x -- --flag`); argparse lo
    # leería como "fin de opciones" y rechazaría todo lo que sigue.
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    # Defensa en profundidad: aunque las capas de pago ya van apagadas en el
    # entry, sin tabla configurada la memoria tampoco podría escribir nada.
    os.environ.pop("FINGERPRINT_TABLE", None)

    entry = _entry_sin_capas_de_pago(args.type)
    archivos = _archivos(args)
    if not archivos:
        print("No hay archivos .pdf/.jpg/.jpeg/.png que analizar.")
        return 1

    raiz = None if args.file else Path(args.dir)
    print(f"{'archivo':38s} {'estructura':16s} {'origen':11s} {'fondo':10s} motivos")
    print("-" * 110)
    resultados, conteo = [], {}
    for path in archivos:
        try:
            resultado = analizar(path, entry, args.type)
        except Exception as e:  # un archivo ilegible no detiene la corrida
            print(f"{str(path)[:38]:38s} ERROR            {e}")
            continue
        resultados.append(resultado)
        veredicto = (resultado["auditoria"].get("structure") or {}).get("verdict", "NO_APLICA")
        conteo[veredicto] = conteo.get(veredicto, 0) + 1
        print(_fila(resultado, raiz))
        if args.detalle:
            _detalle(resultado)

    print("-" * 110)
    total = len(resultados)
    print(f"{total} archivo(s): " + ", ".join(f"{k}={v}" for k, v in sorted(conteo.items())))
    digitales = conteo.get("DIGITAL", 0)
    if total:
        print(f"Con `CAPTURA_FISICA` en on_fail=\"reject\" se rechazarían {digitales} de {total} "
              f"({digitales / total:.0%}); en \"review\" van a revisión.")
    if args.json:
        Path(args.json).write_text(json.dumps(resultados, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"Resultado completo en {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
