#!/usr/bin/env python3
"""¿El embedding sirve para RECUPERAR la referencia correcta? Lo mide con los
recortes reales, antes de activar `references.search` en un tipo.

    pnpm refs:eval                                   (test-docs/nom35/samples)
    pnpm refs:eval -- --dir <carpeta> --cache /tmp/vectores.json

La carpeta tiene una subcarpeta por entidad (número de proveedor) con recortes
`firma*`, `sello*`, `logo*` — la convención de `test-docs/nom35`. Por cada
elemento, cada recorte se usa como consulta contra TODOS los demás (dejándolo
fuera) y se reporta:

  recall@1 / recall@3   la consulta recupera un recorte de SU MISMA entidad
                        entre los 1 / 3 más parecidos (solo consultas cuya
                        entidad tiene otro recorte de ese elemento);
  mismo / otro          coseno contra el mejor recorte de la misma entidad y
                        contra el mejor de otra: la separación entre las dos
                        columnas es lo que hace útil un `min_similarity`;
  min_similarity        umbral sugerido: el punto medio entre el percentil 10
                        de "mismo" y el percentil 90 de "otro". Si se cruzan,
                        no hay umbral que separe y la búsqueda sin clave NO
                        debe activarse para ese elemento.

Usa el MISMO código que la Lambda (`pipeline/reference_embeddings.py`: mismo
preprocesado, mismo proveedor). Costo: una imagen por recorte (~$0.00006 con
Titan; ~$0.01 las 18 carpetas). `--cache` guarda los vectores para repetir el
análisis sin volver a pagar. Solo lectura: no escribe en AWS.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"))

EXTENSIONES = (".png", ".jpg", ".jpeg")


def recortes(raiz: Path, elementos: List[str]) -> List[Dict[str, str]]:
    out = []
    for path in sorted(raiz.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in EXTENSIONES:
            continue
        kind = next((k for k in elementos if path.stem.lower().startswith(k.lower())), None)
        if kind:
            out.append({"path": str(path), "entity": path.parent.name, "kind": kind})
    return out


def percentil(valores: List[float], p: float) -> float:
    if not valores:
        return float("nan")
    orden = sorted(valores)
    return orden[min(int(round(p * (len(orden) - 1))), len(orden) - 1)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=str(REPO / "test-docs" / "nom35" / "samples"))
    parser.add_argument("--kinds", default="FIRMA,SELLO,LOGO")
    parser.add_argument("--cache", help="JSON donde guardar/leer los vectores (por ruta + modelo).")
    parser.add_argument("--region", default=__import__("os").environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    import os

    os.environ.setdefault("AWS_DEFAULT_REGION", args.region)
    from pipeline import reference_embeddings

    proveedor = reference_embeddings.provider_for(None)
    if proveedor is None:
        raise SystemExit("Embeddings desactivados o proveedor desconocido.")
    elementos = [k.strip().upper() for k in args.kinds.split(",") if k.strip()]
    items = recortes(Path(args.dir), elementos)
    if not items:
        raise SystemExit(f"No hay recortes {'/'.join(e.lower() for e in elementos)}* bajo {args.dir}")

    cache: Dict[str, List[float]] = {}
    ruta_cache = Path(args.cache) if args.cache else None
    if ruta_cache and ruta_cache.is_file():
        cache = json.loads(ruta_cache.read_text(encoding="utf-8"))
    faltan = [i for i in items if f"{proveedor.model_id}|{i['path']}" not in cache]
    print(f"{len(items)} recortes de {len({i['entity'] for i in items})} entidades; modelo {proveedor.model_id} "
          f"({proveedor.dimension} dim). Por calcular: {len(faltan)} (~${len(faltan) * 0.00006:.4f}).")
    if faltan and not args.yes and sys.stdin.isatty():
        if input("¿Continuar? (S/n) ").strip().lower() not in ("", "s", "si", "sí", "y"):
            return 0
    for n, item in enumerate(faltan, 1):
        vector, _ = reference_embeddings.embed(Path(item["path"]).read_bytes(), proveedor)
        if vector is None:
            if n == 1:
                raise SystemExit("La primera llamada falló: ¿la cuenta tiene acceso al modelo en Bedrock "
                                 f"({proveedor.model_id}, región {args.region})? Revisa el warning de arriba.")
            print(f"  sin vector: {item['path']}")
            continue
        cache[f"{proveedor.model_id}|{item['path']}"] = [float(v) for v in vector]
    if ruta_cache:
        ruta_cache.write_text(json.dumps(cache), encoding="utf-8")

    for item in items:
        item["vector"] = cache.get(f"{proveedor.model_id}|{item['path']}")
    print(f"\n{'elemento':8s} {'consultas':>9s} {'recall@1':>9s} {'recall@3':>9s}   "
          f"{'mismo p10/p50':>14s}   {'otro p50/p90':>13s}   min_similarity")
    for kind in elementos:
        grupo = [i for i in items if i["kind"] == kind and i["vector"]]
        mismos, otros, en1, en3, consultas = [], [], 0, 0, 0
        for q in grupo:
            resto = sorted(
                ((reference_embeddings.cosine(q["vector"], o["vector"]), o["entity"] == q["entity"])
                 for o in grupo if o is not q), reverse=True)
            propios = [s for s, mismo in resto if mismo]
            ajenos = [s for s, mismo in resto if not mismo]
            if ajenos:
                otros.append(ajenos[0])
            if not propios:
                continue
            consultas += 1
            mismos.append(propios[0])
            en1 += resto[0][1]
            en3 += any(mismo for _, mismo in resto[:3])
        if not consultas:
            print(f"{kind:8s} {'0':>9s}   (ninguna entidad tiene dos recortes de este elemento)")
            continue
        bajo, alto = percentil(mismos, 0.10), percentil(otros, 0.90)
        sugerido = f"{(bajo + alto) / 2:.2f}" if bajo > alto else "NO SEPARA (no activar la búsqueda)"
        print(f"{kind:8s} {consultas:9d} {en1 / consultas:9.2f} {en3 / consultas:9.2f}   "
              f"{bajo:6.3f}/{percentil(mismos, 0.5):6.3f}   {percentil(otros, 0.5):6.3f}/{alto:6.3f}   {sugerido}")
    print("\nrecall@3 es lo que importa para el RANKING por proveedor (se mandan 3 al modelo de visión); "
          "la separación mismo/otro, para la búsqueda sin clave.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
