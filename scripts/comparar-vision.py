#!/usr/bin/env python3
"""Compara modelos de visión en la etapa de autenticidad de un tipo. DE PAGO (Bedrock).

Corre SOLO la etapa de autenticidad — sin extracción, sin Textract, sin tablas —
sobre cada archivo de una carpeta, una vez por modelo, y pone lado a lado lo que
cada uno vio: indicios de edición en la página, elementos presentes, campos que
fallan, errores de llamada, latencia, tokens y costo. Sirve para decidir con
datos qué va en `model_ids.vision` de un tipo.

El primer modelo de `--models` es la referencia contra la que se cuentan las
diferencias. Sin `--models` se compara el modelo que el tipo fija en su catálogo
(`model_ids.vision`, si lo tiene) contra el default de la etapa.

Cada modelo corre SIN respaldo (`vision.fallback_models: []`): un modelo que
falla debe verse como error, no esconderse detrás de otro.

Uso:
    pnpm vision:comparar -- --type ESTADO_CUENTA_PORTAL --dir <carpeta>
    pnpm vision:comparar -- --type CARTA_NOM35 --dir test-docs/nom35/samples/10040 \\
        --models us.anthropic.claude-haiku-4-5-20251001-v1:0,us.amazon.nova-2-lite-v1:0

Costo: una llamada de visión por archivo y modelo con `page_review` solo
(~$0.001-0.003); 2-3 llamadas cuando el tipo declara elementos.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src/document-processing/resources/enrichment-lambda"))

EXTENSIONES = (".pdf", ".jpg", ".jpeg", ".png")


def _entry(type_id: str, modelo: str):
    import document_catalog
    from document_catalog import _parse_entry

    compartido = REPO / "catalogs" / "_shared.json"
    if compartido.is_file():
        document_catalog.load_shared(json.loads(compartido.read_text(encoding="utf-8")))
    rutas = [p for p in (REPO / "catalogs").rglob(f"{type_id}.json")
             if "save" not in p.relative_to(REPO / "catalogs").parts]
    if not rutas:
        raise SystemExit(f"No existe un catálogo activo para {type_id}")
    crudo = copy.deepcopy(json.loads(rutas[0].read_text(encoding="utf-8"))[type_id])
    crudo["model_ids"] = {**(crudo.get("model_ids") or {}), "vision": modelo}
    bloque = crudo.setdefault("authenticity", {})
    vision = bloque.get("vision") if isinstance(bloque.get("vision"), dict) else {}
    bloque["vision"] = {**vision, "fallback_models": []}
    bloque["memory"] = {"enabled": False}
    bloque["references"] = {"enabled": False}
    return _parse_entry(type_id, crudo)


def _modelos_por_defecto(type_id: str) -> list:
    from pipeline import bedrock

    rutas = [p for p in (REPO / "catalogs").rglob(f"{type_id}.json")
             if "save" not in p.relative_to(REPO / "catalogs").parts]
    fijado = (json.loads(rutas[0].read_text(encoding="utf-8"))[type_id].get("model_ids") or {}).get("vision") if rutas else None
    modelos = [bedrock._MULTIMODAL_DEFAULT_MODEL_ID]
    if fijado and fijado not in modelos:
        modelos.append(fijado)
    return modelos


def _correr(entry, ruta: Path, entidades: dict) -> dict:
    from pipeline import authenticity, pricing
    from validators.engine import document_status_from_checks, resolve_field_tree

    datos = ruta.read_bytes()
    es_imagen = ruta.suffix.lower() != ".pdf"
    reader = None
    if not es_imagen:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(datos))
    doc = {"documentType": entry.type_id, "pageStart": 1,
           "pageEnd": 1 if es_imagen else len(reader.pages),
           "_entities": dict(entidades), "_confidences": {}}
    inicio = time.monotonic()
    authenticity.run(doc, entry, authenticity.source_for(datos, reader, es_imagen, raw_bytes=datos), {"documentId": ruta.stem})
    segundos = time.monotonic() - inicio

    calculados = {n for n, s in entry.field_tree.items() if s.computed}
    _, checks, _ = resolve_field_tree(doc["_entities"], doc["_confidences"], entry,
                                      computed_field_names=calculados, default_confidence=0.95)
    propios = [c for c in checks if c.field in calculados]
    auditoria = doc.get("authenticity") or {}
    meta = doc.get("authenticityMeta") or {}
    revision = auditoria.get("pageReview") or {}
    elementos = auditoria.get("elements") or {}
    return {
        "capaVision": (auditoria.get("layers") or {}).get("vision"),
        "error": auditoria.get("visionError"),
        "indicios": revision.get("editSigns") or [],
        "marcasDeAgua": revision.get("watermarks") or [],
        "textoMarcaDeAgua": revision.get("watermarkText") or "",
        "descripcion": revision.get("description") or "",
        "presentes": sorted(k for k, v in elementos.items() if isinstance(v, dict) and v.get("present")),
        "motivosVision": sorted(r["code"] for r in auditoria.get("reasons", []) if r.get("layer") == "vision"),
        "fallan": sorted(c.field for c in propios if not c.valid and not c.skipped),
        "estatus": document_status_from_checks(propios),
        "modeloUsado": meta.get("modelId"),
        "llamadas": meta.get("calls", 0),
        "uso": meta.get("usage") or {},
        "costoUsd": pricing.bedrock_cost(meta.get("usage"), meta.get("modelId")).get("costUsd", 0.0) if meta else 0.0,
        "segundos": round(segundos, 2),
    }


def _firma(r: dict) -> tuple:
    """Lo que el modelo DIJO, para contar diferencias contra la referencia."""
    return (r["capaVision"], tuple(r["indicios"]), tuple(r["marcasDeAgua"]), tuple(r["presentes"]),
            tuple(r["motivosVision"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--type", required=True, help="TYPE_ID con campos de visión (authenticity_source).")
    parser.add_argument("--dir", help="Carpeta a recorrer (recursivo).")
    parser.add_argument("--file", help="Un solo archivo.")
    parser.add_argument("--models", help="IDs de modelo separados por coma; el primero es la referencia.")
    parser.add_argument("--entities", help="JSON con campos extraídos comunes a todos los archivos (p. ej. '{\"TIPO_DOCUMENTO\": \"ESTADO_CUENTA\"}').")
    parser.add_argument("--json", help="Guarda el resultado completo en este archivo.")
    parser.add_argument("--yes", action="store_true", help="No pedir confirmación.")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    os.environ.pop("FINGERPRINT_TABLE", None)
    if args.file:
        archivos = [Path(args.file)]
    elif args.dir:
        archivos = sorted(p for p in Path(args.dir).rglob("*") if p.suffix.lower() in EXTENSIONES)
    else:
        parser.error("indica --dir o --file")
    if not archivos:
        print("No hay archivos que comparar.")
        return 0
    modelos = [m.strip() for m in args.models.split(",") if m.strip()] if args.models else _modelos_por_defecto(args.type)
    entidades = json.loads(args.entities) if args.entities else {}

    print(f"{len(archivos)} archivo(s) x {len(modelos)} modelo(s) = {len(archivos) * len(modelos)} corrida(s) de visión en Bedrock.")
    if not args.yes and sys.stdin.isatty() and input("¿Continuar? [s/N] ").strip().lower() not in ("s", "si", "sí", "y"):
        return 1

    resultados = {m: {} for m in modelos}
    for modelo in modelos:
        entry = _entry(args.type, modelo)
        for ruta in archivos:
            try:
                resultados[modelo][ruta.name] = _correr(entry, ruta, entidades)
            except Exception as e:  # un archivo ilegible no tumba la comparación
                resultados[modelo][ruta.name] = {"capaVision": "ERROR", "error": str(e)[:200], "indicios": [], "marcasDeAgua": [], "presentes": [],
                                                 "motivosVision": [], "fallan": [], "estatus": None, "llamadas": 0,
                                                 "uso": {}, "costoUsd": 0.0, "segundos": 0.0, "descripcion": "", "modeloUsado": None}

    referencia = modelos[0]
    ancho = max(len(p.name) for p in archivos)
    for ruta in archivos:
        print(f"\n{ruta.name}")
        for modelo in modelos:
            r = resultados[modelo][ruta.name]
            dicho = ", ".join(r["indicios"] + r["marcasDeAgua"] + r["motivosVision"]) or "sin indicios"
            if r.get("textoMarcaDeAgua"):
                dicho += f" «{r['textoMarcaDeAgua']}»"
            if r["presentes"]:
                dicho += f" · presentes: {', '.join(r['presentes'])}"
            if r["error"]:
                dicho = f"ERROR: {r['error']}"
            marca = " " if modelo == referencia or _firma(r) == _firma(resultados[referencia][ruta.name]) else "≠"
            print(f"  {marca} {modelo[:46]:46s} {r['segundos']:5.1f}s  {dicho}")
            if r["descripcion"]:
                print(f"      {r['descripcion'][:200]}")

    print("\n" + "-" * 110)
    print(f"{'modelo':48s} {'marcados':>8s} {'errores':>7s} {'≠ ref':>6s} {'s/doc':>6s} {'tok in':>7s} {'tok out':>7s} {'$/doc':>8s}")
    for modelo in modelos:
        filas = list(resultados[modelo].values())
        n = len(filas)
        marcados = sum(1 for r in filas if r["indicios"] or r["motivosVision"])
        errores = sum(1 for r in filas if r["error"] or r["capaVision"] == "ERROR")
        distintos = sum(1 for nombre, r in resultados[modelo].items() if _firma(r) != _firma(resultados[referencia][nombre]))
        print(f"{modelo[:48]:48s} {marcados:8d} {errores:7d} {distintos:6d} "
              f"{sum(r['segundos'] for r in filas) / n:6.1f} "
              f"{sum(r['uso'].get('inputTokens', 0) for r in filas) // n:7d} "
              f"{sum(r['uso'].get('outputTokens', 0) for r in filas) // n:7d} "
              f"{sum(r['costoUsd'] for r in filas) / n:8.4f}")
    print(f"Referencia: {referencia}. `≠ ref` = archivos donde el modelo dijo algo distinto (indicios, elementos o motivos).")

    if args.json:
        Path(args.json).write_text(json.dumps(resultados, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"Resultado completo en {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
