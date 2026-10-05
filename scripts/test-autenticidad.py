#!/usr/bin/env python3
"""Compara la etapa de autenticidad contra su ground truth
(`tests/integration/expected_autenticidad.json`) sobre `test-docs/nom35`.

    pnpm test:autenticidad                 GRATIS: capas de estructura y pixeles
    pnpm test:autenticidad -- --vision     DE PAGO: además la capa de visión (Bedrock)

Modo gratis (default): cero AWS. Verifica, por archivo, el veredicto de
estructura (`FISICO`/`DIGITAL`/`INDETERMINADO`), `physical_capture` y los
motivos duros esperados. Es el chequeo de regresión a correr tras tocar
`pdf_forensics.py`, `pixel_forensics.py` o los umbrales del catálogo.

Modo `--vision`: llama al modelo de visión de verdad (~$0.005-0.01 por carta;
imprime el plan y pide confirmación). Sobre las carpetas ANCLA genuinas exige
que el modelo encuentre los elementos declarados `required` en el catálogo, y
lista lo que opinó de cada recorte. La memoria y las referencias quedan
APAGADAS: una prueba nunca escribe huellas en la tabla real.

Un archivo del ground truth que no esté en disco se omite (test-docs/ no se
commitea); uno cuyo sha256 cambió se reporta, porque ya no es el mismo documento.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"))

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
COSTO_VISION_POR_CARTA = 0.009


def _entry(type_id: str, vision: bool):
    import document_catalog
    from document_catalog import _parse_entry
    from pipeline import pdf_integrity

    compartido = REPO / "catalogs" / "_shared.json"
    if compartido.is_file():
        datos = json.loads(compartido.read_text(encoding="utf-8"))
        document_catalog._FIELD_PRESETS.update(
            {k: v for k, v in (datos.get("_field_presets") or {}).items() if not k.startswith("__")})
    rutas = [p for p in (REPO / "catalogs").rglob(f"{type_id}.json")
             if "save" not in p.relative_to(REPO / "catalogs").parts]
    crudo = copy.deepcopy(json.loads(rutas[0].read_text(encoding="utf-8"))[type_id])
    bloque = crudo.setdefault("authenticity", {})
    if not vision:
        bloque["vision"] = False
    bloque["memory"] = {"enabled": False}
    bloque["references"] = {"enabled": False}
    # La capa de integridad corre siempre (gratis): el ground truth puede fijar
    # qué motivos debe o no debe levantar cada archivo.
    campos = crudo.setdefault("fields", {})
    for señal in pdf_integrity.SIGNALS:
        if not any(isinstance(f, dict) and f.get("authenticity_source") == señal for f in campos.values()):
            campos["_CAL_" + señal.upper().replace(".", "_")] = {
                "type": "string", "required": False, "computed": True,
                "authenticity_source": señal, "hint": "calibración", "validation": []}
    return _parse_entry(type_id, crudo), bloque.get("elements") or {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--expected", default=str(REPO / "tests/integration/expected_autenticidad.json"))
    parser.add_argument("--vision", action="store_true", help="Incluye la capa de visión (Bedrock, de pago).")
    parser.add_argument("--only", help="Solo archivos cuya ruta contenga este texto (p. ej. 10037).")
    parser.add_argument("--yes", action="store_true", help="No pedir confirmación en modo --vision.")
    # pnpm entrega el `--` separador tal cual (`pnpm x -- --flag`); argparse lo
    # leería como "fin de opciones" y rechazaría todo lo que sigue.
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    esperado = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    raiz = REPO / esperado["dataset"]
    casos = {rel: exp for rel, exp in esperado["files"].items()
             if (raiz / rel).is_file() and (not args.only or args.only in rel)}
    if not casos:
        print(f"{YELLOW}No hay archivos del ground truth en {raiz} — nada que comparar.{RESET}")
        return 0
    if args.vision:
        # Solo las carpetas ancla: son las únicas con verdad revisada a ojo.
        casos = {rel: exp for rel, exp in casos.items() if exp.get("verified_by_eye")}
        print(f"Modo --vision: {len(casos)} carta(s) ancla, ~${len(casos) * COSTO_VISION_POR_CARTA:.2f} USD en Bedrock.")
        if not args.yes and input("¿Continuar? [y/N] ").strip().lower() not in ("y", "s", "si", "sí"):
            return 0
    os.environ.pop("FINGERPRINT_TABLE", None)

    from pypdf import PdfReader

    from pipeline import authenticity

    entry, elementos = _entry(esperado["type"], args.vision)
    fallas: List[str] = []
    for rel, exp in sorted(casos.items()):
        datos = (raiz / rel).read_bytes()
        problemas: List[str] = []
        if exp.get("sha256") and hashlib.sha256(datos).hexdigest() != exp["sha256"]:
            problemas.append("el archivo cambió (sha256 distinto al del ground truth)")
        reader = PdfReader(io.BytesIO(datos))
        doc: Dict[str, Any] = {"documentType": esperado["type"], "pageStart": 1, "pageEnd": len(reader.pages)}
        authenticity.run(doc, entry, authenticity.source_for(datos, reader, False), {})
        auditoria = doc["authenticity"]
        estructura = auditoria.get("structure") or {}
        if estructura.get("verdict") != exp["structure"]:
            problemas.append(f"estructura {estructura.get('verdict')} (se esperaba {exp['structure']})")
        if estructura.get("physicalCapture") != exp["physical_capture"]:
            problemas.append(f"physical_capture {estructura.get('physicalCapture')} (se esperaba {exp['physical_capture']})")
        codigos = {r["code"] for r in auditoria.get("reasons") or []}
        for codigo in exp.get("reasons_include") or []:
            if codigo not in codigos:
                problemas.append(f"falta el motivo {codigo}")
        # Invariante del dataset: ninguna captura genuina se acusa de fondo artificial.
        if exp["physical_capture"] is True and (auditoria.get("pixels") or {}).get("naturalBackground") is False:
            problemas.append("captura física marcada con fondo artificial (falso positivo de la capa de pixeles)")
        # Integridad: lo que el ground truth fije, y el invariante de que una
        # captura genuina verificada a ojo nunca levanta un motivo DURO de integridad.
        for codigo in exp.get("integrity_reasons_include") or []:
            if codigo not in codigos:
                problemas.append(f"falta el motivo de integridad {codigo}")
        for codigo in exp.get("integrity_reasons_exclude") or []:
            if codigo in codigos:
                problemas.append(f"sobra el motivo de integridad {codigo}")
        duros = [r["code"] for r in auditoria.get("reasons") or [] if r.get("layer") == "integrity" and r.get("hard")]
        if exp.get("verified_by_eye") and exp["physical_capture"] is True and duros:
            problemas.append(f"captura genuina con motivo duro de integridad: {', '.join(duros)}")
        integridad = (auditoria.get("layers") or {}).get("integrity")
        if integridad == authenticity.LAYER_ERROR:
            problemas.append("la capa de integridad no pudo leer el archivo")

        detalle = ""
        if args.vision:
            if auditoria["layers"].get("vision") != authenticity.LAYER_OK:
                problemas.append(f"la capa de visión no corrió: {auditoria.get('visionError')}")
            vistos = auditoria.get("elements") or {}
            if exp["physical_capture"] is True:
                for kind, spec in elementos.items():
                    if spec.get("required") and not (vistos.get(kind) or {}).get("present"):
                        problemas.append(f"el modelo no encontró {kind}")
            detalle = "  " + "; ".join(
                f"{k}: {'sí' if v.get('present') else 'NO'}"
                + (f" indicios={','.join(v['pastedSigns'])}" if v.get("pastedSigns") else "")
                + (" [imagen incrustada]" if v.get("embeddedImages") else "")
                for k, v in vistos.items())

        marca = f"{GREEN}ok{RESET}" if not problemas else f"{RED}FALLA{RESET}"
        generaciones = (((auditoria.get("integrity") or {}).get("file") or {}).get("chain") or {}).get("generations")
        integridad_txt = f"integridad={integridad or '-'}" + (f" g={generaciones}" if generaciones is not None else "")
        print(f"  {marca}  {rel:22s} {estructura.get('verdict', '-'):14s} {auditoria.get('verdict', '-'):14s} "
              f"{integridad_txt:20s}{detalle}")
        for problema in problemas:
            print(f"        - {problema}")
            fallas.append(f"{rel}: {problema}")

    print(f"\n{len(casos) - len({f.split(':')[0] for f in fallas})}/{len(casos)} archivo(s) conforme al ground truth.")
    return 1 if fallas else 0


if __name__ == "__main__":
    raise SystemExit(main())
