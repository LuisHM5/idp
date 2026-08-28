#!/usr/bin/env python3
"""Calibra los marcadores de `page_selection` SIN gastar un centavo.

Lee el texto NATIVO de cada PDF con pypdf (gratis, sin Textract) y corre el
mismo `pipeline/page_selector.py` que usará la Lambda, para responder dos
preguntas antes de tocar el pipeline:

  1. ¿Qué páginas seleccionaría el catálogo actual?
  2. ¿Cuántas páginas dejaríamos de mandar a Bedrock/Textract?

No llama a AWS: no necesita credenciales y las páginas escaneadas (sin capa
de texto) salen listadas aparte, porque sobre ellas el selector de texto no
puede opinar — ésas las resuelve el sondeo de la Fase 2.

Uso:
    python scripts/analizar-seleccion-paginas.py
    python scripts/analizar-seleccion-paginas.py --dir "test-docs/full-dataset-pedimentos"
    python scripts/analizar-seleccion-paginas.py --file test-docs/normal_pend.pdf
    python scripts/analizar-seleccion-paginas.py --type PEDIMENTO_ADUANAL
    python scripts/analizar-seleccion-paginas.py --min-score 3 --max-ratio 0.5
        # prueba otros umbrales sin editar el catálogo
    python scripts/analizar-seleccion-paginas.py --show-pages
        # además, imprime el score de CADA página (para depurar un marcador)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"))

DEFAULT_DIR = REPO / "test-docs" / "pedimentos docs" / "docs"
DEFAULT_TYPE = "PEDIMENTO_ADUANAL"


def _load_spec(type_id: str, args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    """The type's `page_selection`, with CLI overrides applied on top."""
    from document_catalog import get_type_entry
    from pipeline import page_selector

    entry = get_type_entry(type_id)
    spec = dict(page_selector.spec_for(entry) or {})
    if args.min_score is not None:
        spec["min_score"] = args.min_score
    if args.max_ratio is not None:
        spec["max_ratio"] = args.max_ratio
    if args.follow_pages is not None:
        spec["follow_pages"] = args.follow_pages
    return spec or None


def _analyze(pdf: Path, spec: Dict[str, Any], show_pages: bool) -> Optional[Dict[str, Any]]:
    from pipeline.page_selector import select
    from pipeline.pdf_pages import load_pages

    try:
        # use_ocr=False es lo que mantiene esto en $0: las páginas escaneadas
        # vuelven con texto vacío en vez de dispararle una llamada a Textract.
        pages = load_pages(pdf.read_bytes(), use_ocr=False)
    except Exception as e:
        print(f"  !! no se pudo leer: {e}")
        return None

    native = [p for p in pages if p.source == "native"]
    scanned = [p for p in pages if p.source != "native"]
    selection = select(pages, spec)

    print(f"  páginas: {len(pages)}  (nativas {len(native)}, sin texto {len(scanned)})")
    if selection.fail_open:
        print(f"  FAIL-OPEN → se procesarían las {len(pages)}: {selection.reason}")
    else:
        ahorro = len(selection.skipped) / len(pages) * 100
        print(f"  seleccionadas: {selection.selected}")
        print(f"  descartadas:   {len(selection.skipped)} de {len(pages)}  ({ahorro:.0f}% menos páginas a Bedrock)")
    if scanned:
        print(f"  sin capa de texto (las decide el sondeo de Fase 2): {[p.number for p in scanned]}")
    if show_pages:
        for s in selection.scores:
            marca = "*" if s.number in selection.selected else " "
            print(f"    {marca} pág {s.number:>3}  score={s.score}  {', '.join(s.matched[:4])}")

    return {
        "total": len(pages),
        "selected": len(pages) if selection.fail_open else len(selection.selected),
        "fail_open": selection.fail_open,
        "scanned": len(scanned),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="carpeta con PDFs")
    ap.add_argument("--file", type=Path, help="un solo PDF (ignora --dir)")
    ap.add_argument("--type", default=DEFAULT_TYPE, help="TYPE_ID cuyo page_selection se prueba")
    ap.add_argument("--min-score", type=int, help="override de min_score")
    ap.add_argument("--max-ratio", type=float, help="override de max_ratio (0-1)")
    ap.add_argument("--follow-pages", type=int, help="override de follow_pages")
    ap.add_argument("--show-pages", action="store_true", help="score de cada página")
    args = ap.parse_args()

    spec = _load_spec(args.type, args)
    if not spec or not spec.get("markers"):
        print(f"El tipo {args.type} no declara `page_selection.markers`.")
        print("Agrégalo a catalogs/<TIPO>.json (y corre `pnpm build`) o pruébalo con --min-score/--max-ratio.")
        return 1

    print(f"Tipo: {args.type}")
    print(f"Marcadores: {len(spec['markers'])}   min_score={spec.get('min_score', 2)}   "
          f"follow_pages={spec.get('follow_pages', 1)}   "
          f"max_ratio={spec.get('max_ratio', 0.6):.0%}\n")

    pdfs: List[Path] = [args.file] if args.file else sorted(args.dir.glob("*.pdf"))
    if not pdfs:
        print(f"No hay PDFs en {args.dir}")
        return 1

    stats: List[Dict[str, Any]] = []
    for pdf in pdfs:
        print(f"{pdf.name}")
        result = _analyze(pdf, spec, args.show_pages)
        if result:
            stats.append(result)
        print()

    if stats:
        total = sum(s["total"] for s in stats)
        selected = sum(s["selected"] for s in stats)
        fail_open = sum(1 for s in stats if s["fail_open"])
        print("=" * 60)
        print(f"TOTAL: {selected}/{total} páginas se mandarían a Bedrock "
              f"({(1 - selected / total) * 100:.0f}% menos)")
        print(f"Documentos en fail-open (ningún marcador alcanzó el umbral): {fail_open}/{len(stats)}")
        if fail_open:
            print("  → esos documentos se procesan completos, como hoy. Si son muchos,")
            print("    los marcadores están mal calibrados: revísalos con --show-pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
