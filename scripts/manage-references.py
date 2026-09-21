#!/usr/bin/env python3
"""Alta, consulta y baja de las REFERENCIAS de autenticidad de un proveedor:
las firmas, sellos y logotipos VÁLIDOS contra los que se compara cada documento
que ese proveedor entregue (etapa `authenticity`, ver
documentacion/AUTENTICIDAD_Y_REFERENCIAS.md — la guía completa, con ejemplos
de cómo recortar y clasificar bien).

Un proveedor puede tener VARIAS de cada una (dos sucursales = dos sellos, tres
personas autorizadas = tres firmas, membrete viejo y nuevo = dos logos). Todas
las activas son válidas: al documento le basta corresponder a UNA.

    pnpm refs:register -- --key 10040 --dir referencias/10040            (dev)
    pnpm refs:register -- --key 10040 --dir referencias/10040 --env prod
    pnpm refs:register -- --key 10021 --file firma.png --kind FIRMA --label "Pablo Ontiveros"
    pnpm refs:list     -- --key 10040
    pnpm refs:list                                   (todos los proveedores con referencias)
    pnpm refs:remove   -- --key 10040 --kind SELLO --ref planta-envasadora
    pnpm refs:remove   -- --key 10040 --kind SELLO --ref planta-envasadora --delete

Cómo se clasifica cada archivo de `--dir` (en este orden):
  1. por SUBCARPETA:   <dir>/firma/*.png   <dir>/sello/*.png   <dir>/logo/*.png
  2. por PREFIJO:      <dir>/firma-julio-lemus.png   <dir>/sello_planta.png   <dir>/logo1.png
El nombre del elemento sale del CATÁLOGO del tipo (`--type`, sus
`authenticity.elements`), no de este script: FIRMA -> "firma", SELLO -> "sello".
Lo que sigue al prefijo es la ETIQUETA legible (quién firma, qué sucursal).
Un archivo que no se puede clasificar se reporta y NO se sube.

Dónde queda cada cosa:
  imagen   s3://<bucket>/references/<scope>/<key>/<KIND>/<refId>.<ext>
  índice   tabla document-validation-fingerprints[-dev], Pk = R#<scope>#<key>

Baja: `refs:remove` DESACTIVA (Active=false, reversible volviendo a registrar);
con `--delete` borra la fila y la imagen. Un documento ya procesado no cambia.

Toda operación que escribe muestra el plan y pide confirmación (S/n), salvo
`--yes` o `--dry-run`. `--env` es dev por defecto, igual que el deploy.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"))
sys.path.insert(0, str(REPO / "scripts"))

EXTENSIONES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}
MAX_BYTES = 3_500_000          # tope de Bedrock por imagen, con margen
MIN_LADO_PX = 120              # por debajo, no hay trazo que comparar
MIN_LADO_SIN_HUELLA_PX = 80    # un logo (sin huella de tinta) suele ser ancho y bajito
MAX_LADO_PX = 2200             # por encima, casi seguro es la hoja entera
PARECIDO_DUPLICADO = 0.97


# ------------------------------------------------------------------ catálogo


def elementos_del_tipo(type_id: str) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """(elementos declarados, scope) del bloque `authenticity` del tipo."""
    import json

    candidatos = [p for p in (REPO / "catalogs").rglob(f"{type_id}.json")
                  if "save" not in p.relative_to(REPO / "catalogs").parts]
    if len(candidatos) != 1:
        raise SystemExit(f"No hay exactamente un catálogo activo para {type_id}: {candidatos}")
    bloque = json.loads(candidatos[0].read_text(encoding="utf-8"))[type_id].get("authenticity") or {}
    elementos = bloque.get("elements") or {}
    if not elementos:
        raise SystemExit(f"{type_id} no declara `authenticity.elements`.")
    return elementos, str((bloque.get("references") or {}).get("scope") or "DEFAULT")


def slug(texto: str) -> str:
    plano = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", plano.lower()).strip("-")


def clasificar(path: Path, raiz: Path, elementos: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(KIND, etiqueta) de un archivo, por subcarpeta o por prefijo."""
    por_prefijo = {kind.lower(): kind for kind in elementos}
    partes = path.relative_to(raiz).parts
    if len(partes) > 1 and partes[0].lower() in por_prefijo:
        return por_prefijo[partes[0].lower()], path.stem
    nombre = path.stem.lower()
    for prefijo, kind in sorted(por_prefijo.items(), key=lambda kv: -len(kv[0])):
        if nombre.startswith(prefijo):
            resto = path.stem[len(prefijo):].strip(" -_.")
            return kind, resto or path.stem
    return None


# ------------------------------------------------------------------ revisión


def revisar(path: Path, kind: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Abre el recorte, calcula su huella con el MISMO código de la Lambda y
    junta los problemas de calidad. `errores` impiden el alta; `avisos` no."""
    from pipeline import page_raster, pixel_forensics

    datos = path.read_bytes()
    # El formato REAL, por los primeros bytes: la extensión miente seguido (JPEG
    # guardados como .png), y un Content-Type equivocado confunde a quien lo lea.
    real = "png" if datos[:8] == b"\x89PNG\r\n\x1a\n" else "jpeg" if datos[:3] == b"\xff\xd8\xff" else None
    ficha: Dict[str, Any] = {"path": path, "kind": kind, "datos": datos, "errores": [], "avisos": [],
                             "sha256": hashlib.sha256(datos).hexdigest(),
                             "formato": real or EXTENSIONES[path.suffix.lower()], "grid": None}
    if real and real != EXTENSIONES[path.suffix.lower()]:
        ficha["avisos"].append(f"el archivo es {real.upper()} aunque se llame {path.suffix}: se registra como {real.upper()}")
    if len(datos) > MAX_BYTES:
        ficha["errores"].append(f"pesa {len(datos) / 1e6:.1f} MB (máx {MAX_BYTES / 1e6:.1f}); recorta o comprime")
    imagen = page_raster.decode_image(datos)
    if imagen is None:
        ficha["errores"].append("no se pudo abrir como imagen")
        return ficha
    alto, ancho = imagen.shape[:2]
    ficha["ancho"], ficha["alto"] = ancho, alto
    minimo = MIN_LADO_PX if spec.get("fingerprint", True) is not False else MIN_LADO_SIN_HUELLA_PX
    if min(alto, ancho) < minimo:
        ficha["errores"].append(f"muy chica ({ancho}x{alto}); escanea a 200-300 dpi")
    if min(alto, ancho) > MAX_LADO_PX:
        ficha["errores"].append(f"muy grande ({ancho}x{alto}): parece la hoja completa; recorta SOLO el elemento")
    if spec.get("fingerprint", True) is not False:
        ficha["grid"] = pixel_forensics.fingerprint(imagen)
        if ficha["grid"] is None:
            ficha["errores"].append("no se distingue tinta sobre el fondo (¿imagen en blanco o fondo oscuro?)")
        else:
            tinta = float(pixel_forensics.ink_mask(imagen).mean())
            if tinta > 0.45:
                ficha["avisos"].append(f"{tinta:.0%} de la imagen es 'tinta': ¿trae texto o fondo de más alrededor?")
    return ficha


# ------------------------------------------------------------------ AWS


def _recursos(args) -> Tuple[Any, Any, str, str]:
    import boto3
    from _report_lib import stack_id

    region = args.region
    tabla = args.table or ("document-validation-fingerprints" + ("" if args.env == "prod" else "-dev"))
    bucket = args.bucket
    if not bucket and args.command != "list":
        stack = args.stack or stack_id(args.env)
        salidas = boto3.client("cloudformation", region_name=region).describe_stacks(StackName=stack)
        bucket = next((o["OutputValue"] for o in salidas["Stacks"][0].get("Outputs", [])
                       if o["OutputKey"] == "DocumentBucketName"), None)
        if not bucket:
            raise SystemExit(f"El stack {stack} no expone `DocumentBucketName` (¿falta desplegar?). Usa --bucket.")
    return (boto3.resource("dynamodb", region_name=region).Table(tabla),
            boto3.client("s3", region_name=region), bucket or "", tabla)


def _existentes(tabla, scope: str, key: str) -> List[Dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    from pipeline import fingerprint_store

    items, kwargs = [], {"KeyConditionExpression": Key("Pk").eq(fingerprint_store.reference_pk(scope, key))}
    while True:
        pagina = tabla.query(**kwargs)
        items.extend(pagina.get("Items") or [])
        if not pagina.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = pagina["LastEvaluatedKey"]


def confirmar(args, pregunta: str) -> bool:
    if args.dry_run:
        print("\n--dry-run: no se escribió nada.")
        return False
    if args.yes or not sys.stdin.isatty():
        return True
    return input(f"\n{pregunta} (S/n) ").strip().lower() in ("", "s", "si", "sí", "y")


# ------------------------------------------------------------------ comandos


def cmd_register(args) -> int:
    from pipeline import fingerprint_store, pixel_forensics

    elementos, scope_catalogo = elementos_del_tipo(args.type)
    scope = args.scope or scope_catalogo
    if args.file:
        if not args.kind:
            raise SystemExit("--file necesita --kind (uno de: " + ", ".join(elementos) + ")")
        if args.kind not in elementos:
            raise SystemExit(f"--kind {args.kind} no es un elemento de {args.type}: {', '.join(elementos)}")
        candidatos = [(Path(args.file), args.kind, args.label or Path(args.file).stem)]
        ignorados: List[Path] = []
    else:
        raiz = Path(args.dir)
        if not raiz.is_dir():
            raise SystemExit(f"No existe la carpeta: {raiz}")
        candidatos, ignorados = [], []
        for path in sorted(p for p in raiz.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONES):
            clase = clasificar(path, raiz, elementos)
            if clase:
                candidatos.append((path, clase[0], clase[1]))
            else:
                ignorados.append(path)

    try:
        tabla, s3, bucket, nombre_tabla = _recursos(args)
        ya = _existentes(tabla, scope, args.key)
    except Exception as e:
        if not args.dry_run:
            raise
        # Un --dry-run sirve también sin AWS (sin credenciales, o antes del
        # primer deploy): revisa y clasifica los recortes, que es lo que más
        # falla, y solo se pierde la comparación contra lo ya registrado.
        print(f"(sin acceso a AWS — {type(e).__name__}: no se comparó contra lo ya registrado)")
        tabla = s3 = None
        bucket, nombre_tabla, ya = "", "sin conexión", []
    por_sha = {i.get("Sha256"): i for i in ya if i.get("Active") is not False}

    plan, vistos = [], {}
    print(f"\nProveedor {scope} {args.key} — tipo {args.type} — ambiente {args.env} ({nombre_tabla})\n")
    for path, kind, etiqueta in candidatos:
        ficha = revisar(path, kind, elementos[kind])
        ficha["etiqueta"] = etiqueta
        if etiqueta.strip().isdigit():
            ficha["avisos"].append(
                f"etiqueta '{etiqueta}' no dice nada: nombra el archivo con quién firma o qué sucursal "
                f"(p. ej. {kind.lower()}-<quien-o-sucursal>.png) para que la revisión sea legible")
        ficha["refId"] = slug(etiqueta) or ficha["sha256"][:10]
        estado = "ALTA"
        if ficha["sha256"] in por_sha:
            estado = f"YA REGISTRADA como {por_sha[ficha['sha256']]['Sk']}"
        elif ficha["sha256"] in vistos:
            estado = f"REPETIDA (mismo archivo que {vistos[ficha['sha256']].name})"
        elif ficha["grid"] and not args.force:
            parecida = next(
                (i for i in ya if i.get("Active") is not False and str(i.get("Sk", "")).startswith(kind + "#")
                 and pixel_forensics.grid_similarity(ficha["grid"], fingerprint_store._binary(i.get("InkGrid")))
                 >= PARECIDO_DUPLICADO), None)
            previa = next((p for p in plan if p["kind"] == kind and p["grid"] and
                           pixel_forensics.grid_similarity(ficha["grid"], p["grid"]) >= PARECIDO_DUPLICADO), None)
            if parecida is not None:
                estado = f"MISMA TINTA que {parecida['Sk']} (usa --force para subirla igual)"
            elif previa is not None:
                estado = f"MISMA TINTA que {previa['path'].name} (una sola basta)"
        if ficha["errores"]:
            estado = "RECHAZADA: " + "; ".join(ficha["errores"])
        vistos.setdefault(ficha["sha256"], path)
        print(f"  {kind:6s} {ficha['refId'][:28]:28s} {path.name[:30]:30s} "
              f"{ficha.get('ancho', '?')}x{ficha.get('alto', '?'):<6} {estado}")
        for aviso in ficha["avisos"]:
            print(f"         aviso: {aviso}")
        if estado == "ALTA":
            plan.append(ficha)
    for path in ignorados:
        print(f"  ??     {path.name}: no se pudo clasificar (usa subcarpetas o prefijos "
              f"{'/'.join(k.lower() for k in elementos)})")

    if not plan:
        print("\nNada que registrar.")
        return 0
    por_kind = {k: sum(1 for p in plan if p["kind"] == k) for k in elementos}
    print("\nSe registrarán: " + ", ".join(f"{n} {k}" for k, n in por_kind.items() if n))
    if not confirmar(args, f"¿Registrar {len(plan)} referencia(s) en {args.env}?"):
        return 0

    usados = {str(i.get("Sk")) for i in ya}
    for ficha in plan:
        ref_id, n = ficha["refId"], 2
        while f"{ficha['kind']}#{ref_id}" in usados:  # otra imagen con la misma etiqueta
            ref_id, n = f"{ficha['refId']}-{n}", n + 1
        usados.add(f"{ficha['kind']}#{ref_id}")
        extension = "png" if ficha["formato"] == "png" else "jpg"
        s3_key = f"references/{scope}/{args.key}/{ficha['kind']}/{ref_id}.{extension}"
        s3.put_object(Bucket=bucket, Key=s3_key, Body=ficha["datos"],
                      ContentType=f"image/{ficha['formato']}")
        tabla.put_item(Item={
            "Pk": fingerprint_store.reference_pk(scope, args.key), "Sk": f"{ficha['kind']}#{ref_id}",
            "Label": ficha["etiqueta"], "S3Key": s3_key, "Format": ficha["formato"],
            "Sha256": ficha["sha256"], "Width": ficha["ancho"], "Height": ficha["alto"],
            **({"InkGrid": ficha["grid"]} if ficha["grid"] else {}),
            "Active": True, "CreatedAt": datetime.now(timezone.utc).isoformat(),
            "CreatedBy": getpass.getuser(), "SourceFile": ficha["path"].name, "ForType": args.type,
        })
        print(f"  ok  {ficha['kind']}#{ref_id}  ->  s3://{bucket}/{s3_key}")
    print(f"\nListo. Desde ahora los documentos con metadata `proveedor={args.key}` se comparan contra ellas.")
    return 0


def cmd_list(args) -> int:
    tabla, _, _, nombre_tabla = _recursos(args)
    if args.key:
        _, scope_catalogo = elementos_del_tipo(args.type)
        items = _existentes(tabla, args.scope or scope_catalogo, args.key)
    else:
        from boto3.dynamodb.conditions import Attr

        items, kwargs = [], {"FilterExpression": Attr("Pk").begins_with("R#")}
        while True:
            pagina = tabla.scan(**kwargs)
            items.extend(pagina.get("Items") or [])
            if not pagina.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = pagina["LastEvaluatedKey"]
    if not items:
        print(f"Sin referencias registradas ({nombre_tabla}).")
        return 0
    actual = None
    for item in sorted(items, key=lambda i: (i["Pk"], i["Sk"])):
        if item["Pk"] != actual:
            actual = item["Pk"]
            _, scope, key = actual.split("#", 2)
            print(f"\n{scope} {key}")
        estado = "activa" if item.get("Active") is not False else "INACTIVA"
        print(f"  {item['Sk']:40s} {estado:9s} {item.get('Width', '?')}x{item.get('Height', '?'):<6} "
              f"{str(item.get('CreatedAt', ''))[:10]}  {item.get('Label', '')}")
    return 0


def cmd_remove(args) -> int:
    from pipeline import fingerprint_store

    if not (args.key and args.kind and args.ref):
        raise SystemExit("refs:remove necesita --key, --kind y --ref (ver `pnpm refs:list -- --key ...`)")
    _, scope_catalogo = elementos_del_tipo(args.type)
    scope = args.scope or scope_catalogo
    tabla, s3, bucket, _ = _recursos(args)
    llave = {"Pk": fingerprint_store.reference_pk(scope, args.key), "Sk": f"{args.kind}#{args.ref}"}
    item = tabla.get_item(Key=llave).get("Item")
    if not item:
        raise SystemExit(f"No existe {llave['Sk']} para {scope} {args.key}.")
    accion = "BORRAR (fila + imagen, irreversible)" if args.delete else "DESACTIVAR"
    print(f"{accion}: {scope} {args.key} {llave['Sk']} — {item.get('Label', '')}")
    if not confirmar(args, "¿Continuar?"):
        return 0
    if args.delete:
        if item.get("S3Key"):
            s3.delete_object(Bucket=bucket, Key=item["S3Key"])
        tabla.delete_item(Key=llave)
    else:
        tabla.update_item(Key=llave, UpdateExpression="SET Active = :f, DeactivatedAt = :t",
                          ExpressionAttributeValues={":f": False, ":t": datetime.now(timezone.utc).isoformat()})
    print("Hecho.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["register", "list", "remove"])
    parser.add_argument("--key", help="Identificador del proveedor (lo que el upload manda en metadata `proveedor`).")
    parser.add_argument("--dir", help="Carpeta con los recortes clasificados (register).")
    parser.add_argument("--file", help="Un solo recorte (register); requiere --kind.")
    parser.add_argument("--kind", help="Elemento: FIRMA, SELLO, LOGO... (los del catálogo del tipo).")
    parser.add_argument("--label", help="Etiqueta legible con --file (quién firma, qué sucursal).")
    parser.add_argument("--ref", help="refId a dar de baja (remove).")
    parser.add_argument("--delete", action="store_true", help="remove: borra en vez de desactivar.")
    parser.add_argument("--force", action="store_true", help="register: sube aunque ya haya una con la misma tinta.")
    parser.add_argument("--type", default="CARTA_NOM35", help="Tipo cuyo catálogo define elementos y scope.")
    parser.add_argument("--scope", help="Scope explícito (por defecto el `references.scope` del catálogo).")
    parser.add_argument("--env", choices=["dev", "prod"], default="dev")
    parser.add_argument("--stack", help="Stack explícito (gana sobre --env).")
    parser.add_argument("--table", help="Tabla de huellas explícita.")
    parser.add_argument("--bucket", help="Bucket explícito (si no, se lee del output DocumentBucketName).")
    parser.add_argument("--region", default=__import__("os").environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--dry-run", action="store_true", help="Muestra el plan y no escribe nada.")
    parser.add_argument("--yes", action="store_true", help="No pedir confirmación.")
    # pnpm entrega el `--` separador tal cual (`pnpm x -- --flag`); argparse lo
    # leería como "fin de opciones" y rechazaría todo lo que sigue.
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])
    if args.command == "register" and not args.key:
        parser.error("register necesita --key")
    if args.command == "register" and not (args.dir or args.file):
        parser.error("register necesita --dir o --file")
    return {"register": cmd_register, "list": cmd_list, "remove": cmd_remove}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
