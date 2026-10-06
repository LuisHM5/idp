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
    pnpm refs:register -- --root referencias/ --dry-run     (en lote: una subcarpeta por proveedor)
    pnpm refs:register -- --key 10021 --file firma.png --kind FIRMA --label "Pablo Ontiveros"
    pnpm refs:list     -- --key 10040
    pnpm refs:list                                   (todos los proveedores con referencias)
    pnpm refs:remove   -- --key 10040 --kind SELLO --ref planta-envasadora
    pnpm refs:remove   -- --key 10040 --kind SELLO --ref planta-envasadora --delete
    pnpm refs:reindex                                (embeddings + índice vectorial desde la tabla)
    pnpm refs:search   -- --file recorte.png --kind SELLO   (¿de quién es este sello?)

Las referencias son de la ENTIDAD (`scope` + clave: PROVEEDOR 10040), no de un
tipo de documento: un logotipo se registra una vez y lo usa todo tipo que
declare ese scope. `--type` solo sirve para tomar su scope por defecto.

Este script y la API REST (`/references/...`, `references_api.py`) llaman el
MISMO código (`pipeline/reference_registry.py`): misma revisión de calidad,
mismas reglas de duplicados, mismo embedding.

Cómo se clasifica cada archivo de `--dir` (en este orden):
  1. por SUBCARPETA:   <dir>/firma/*.png   <dir>/sello/*.png   <dir>/logo/*.png
  2. por PREFIJO:      <dir>/firma-julio-lemus.png   <dir>/sello_planta.png   <dir>/logo1.png
El nombre del elemento sale del CATÁLOGO (los `authenticity.elements` de los
tipos que declaran ese scope), no de este script: FIRMA -> "firma", SELLO -> "sello".
Lo que sigue al prefijo es la ETIQUETA legible (quién firma, qué sucursal).
Un archivo que no se puede clasificar se reporta y NO se sube.

Dónde queda cada cosa:
  imagen   s3://<bucket>/references/<scope>/<key>/<KIND>/<refId>.<ext>
  índice   tabla document-validation-fingerprints[-dev], Pk = R#<scope>#<key>

Baja: `refs:remove` DESACTIVA (queda INACTIVE; volver a registrar el recorte crea una fila nueva);
con `--delete` borra la fila y la imagen. Un documento ya procesado no cambia.

Toda operación que escribe muestra el plan y pide confirmación (S/n), salvo
`--yes` o `--dry-run`. `--env` es dev por defecto, igual que el deploy.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"))
sys.path.insert(0, str(REPO / "scripts"))

EXTENSIONES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}


# ------------------------------------------------------------------ catálogo


def scopes_del_catalogo() -> Tuple[Dict[str, Dict[str, Dict[str, Any]]], Dict[str, str]]:
    """({scope: {KIND: elemento}}, {TYPE_ID: scope}) leyendo `catalogs/` del
    repo — la misma forma que la Lambda saca del bundle."""
    import document_catalog
    from pipeline import authenticity

    compartido = REPO / "catalogs" / "_shared.json"
    if compartido.is_file():
        document_catalog.load_shared(json.loads(compartido.read_text(encoding="utf-8")))
    entradas, por_tipo = [], {}
    for ruta in sorted((REPO / "catalogs").rglob("*.json")):
        if ruta.name.startswith("_") or "save" in ruta.relative_to(REPO / "catalogs").parts:
            continue
        for type_id, crudo in json.loads(ruta.read_text(encoding="utf-8")).items():
            if not isinstance(crudo, dict) or not isinstance(crudo.get("authenticity"), dict):
                continue
            entrada = types.SimpleNamespace(authenticity=crudo["authenticity"])
            entradas.append(entrada)
            propios = authenticity.reference_scopes([entrada])
            if propios:
                por_tipo[type_id] = next(iter(propios))
    return authenticity.reference_scopes(entradas), por_tipo


def resolver_scope(args) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    """(scope, sus elementos). `--scope` manda; si no, el del `--type`."""
    scopes, por_tipo = scopes_del_catalogo()
    scope = args.scope or por_tipo.get(args.type)
    if not scope:
        raise SystemExit(f"{args.type} no declara `authenticity.references`; usa --scope "
                         f"({', '.join(sorted(scopes)) or 'ningún tipo declara referencias'}).")
    if scope not in scopes:
        raise SystemExit(f"Ningún tipo declara el scope '{scope}'. Válidos: {', '.join(sorted(scopes))}.")
    return scope, scopes[scope]


def clave_valida(args) -> str:
    from pipeline import reference_registry

    try:
        return reference_registry.validate_key(args.key)
    except reference_registry.RegistryError as e:
        raise SystemExit(f"--key: {e.message}")


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


# ------------------------------------------------------------------ AWS


def _recursos(args, con_bucket: bool = True):
    """El registro del ambiente (`pipeline.reference_registry.Registry`) y el
    nombre de su tabla. El bucket y el índice vectorial salen de los outputs
    del stack; un stack desplegado antes del índice simplemente no lo tiene."""
    import os

    import boto3
    from _report_lib import stack_id

    from pipeline import reference_embeddings, reference_registry

    region = args.region
    tabla = args.table or ("document-validation-fingerprints" + ("" if args.env == "prod" else "-dev"))
    salidas: Dict[str, str] = {}
    if con_bucket or not args.bucket:
        try:
            stack = args.stack or stack_id(args.env)
            respuesta = boto3.client("cloudformation", region_name=region).describe_stacks(StackName=stack)
            salidas = {o["OutputKey"]: o["OutputValue"] for o in respuesta["Stacks"][0].get("Outputs", [])}
        except Exception:
            if con_bucket and not args.bucket:
                raise
    bucket = args.bucket or salidas.get("DocumentBucketName") or ""
    if con_bucket and not bucket:
        raise SystemExit("El stack no expone `DocumentBucketName` (¿falta desplegar?). Usa --bucket.")
    if salidas.get("ReferenceVectorBucketName") and salidas.get("ReferenceVectorIndexBase"):
        os.environ.setdefault("REFERENCE_VECTOR_BUCKET", salidas["ReferenceVectorBucketName"])
        os.environ.setdefault("REFERENCE_VECTOR_INDEX", salidas["ReferenceVectorIndexBase"])
    os.environ.setdefault("AWS_DEFAULT_REGION", region)
    proveedor = None if getattr(args, "sin_embeddings", False) else reference_embeddings.provider_for(None)
    registro = reference_registry.Registry(
        table=boto3.resource("dynamodb", region_name=region).Table(tabla),
        s3=boto3.client("s3", region_name=region), bucket=bucket, provider=proveedor,
    )
    return registro, tabla


def confirmar(args, pregunta: str) -> bool:
    if args.dry_run:
        print("\n--dry-run: no se escribió nada.")
        return False
    if args.yes or not sys.stdin.isatty():
        return True
    return input(f"\n{pregunta} (S/n) ").strip().lower() in ("", "s", "si", "sí", "y")


# ------------------------------------------------------------------ comandos


def cmd_register(args) -> int:
    from pipeline import reference_registry

    scope, elementos = resolver_scope(args)
    key = clave_valida(args)
    if args.file:
        if not args.kind:
            raise SystemExit("--file necesita --kind (uno de: " + ", ".join(elementos) + ")")
        if args.kind not in elementos:
            raise SystemExit(f"--kind {args.kind} no es un elemento de {scope}: {', '.join(elementos)}")
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
        registro, nombre_tabla = _recursos(args)
        ya = registro.items(scope, key)
    except Exception as e:
        if not args.dry_run:
            raise
        # Un --dry-run sirve también sin AWS (sin credenciales, o antes del
        # primer deploy): revisa y clasifica los recortes, que es lo que más
        # falla, y solo se pierde la comparación contra lo ya registrado.
        print(f"(sin acceso a AWS — {type(e).__name__}: no se comparó contra lo ya registrado)")
        registro, nombre_tabla, ya = None, "sin conexión", []

    plan, vistos = [], {}
    print(f"\n{scope} {key} — ambiente {args.env} ({nombre_tabla})\n")
    for path, kind, etiqueta in candidatos:
        datos = path.read_bytes()
        ficha = reference_registry.review(datos, elementos[kind], EXTENSIONES.get(path.suffix.lower()))
        ficha.update({"path": path, "kind": kind, "datos": datos, "etiqueta": etiqueta})
        avisos = [a["message"] for a in ficha["warnings"]]
        if etiqueta.strip().isdigit():
            avisos.append(
                f"etiqueta '{etiqueta}' no dice nada: nombra el archivo con quién firma o qué sucursal "
                f"(p. ej. {kind.lower()}-<quien-o-sucursal>.png) para que la revisión sea legible")
        ficha["refId"] = reference_registry.slug(etiqueta) or ficha["sha256"][:10]
        estado = "ALTA"
        duplicada = reference_registry.find_duplicate(ficha, kind, ya)
        if duplicada and duplicada["reason"] == "SAME_FILE":
            estado = f"YA REGISTRADA como {duplicada['item']['Sk']}"
        elif ficha["sha256"] in vistos:
            estado = f"REPETIDA (mismo archivo que {vistos[ficha['sha256']].name})"
        elif duplicada and not args.force:
            estado = f"MISMA TINTA que {duplicada['item']['Sk']} (usa --force para subirla igual)"
        elif ficha["grid"] and not args.force:
            from pipeline import pixel_forensics

            previa = next((p for p in plan if p["kind"] == kind and p["grid"] and pixel_forensics.grid_similarity(
                ficha["grid"], p["grid"]) >= reference_registry.DUPLICATE_SIMILARITY), None)
            if previa is not None:
                estado = f"MISMA TINTA que {previa['path'].name} (una sola basta)"
        if ficha["errors"]:
            estado = "RECHAZADA: " + "; ".join(e["message"] for e in ficha["errors"])
        vistos.setdefault(ficha["sha256"], path)
        print(f"  {kind:6s} {ficha['refId'][:28]:28s} {path.name[:30]:30s} "
              f"{ficha.get('width') or '?'}x{str(ficha.get('height') or '?'):<6} {estado}")
        for aviso in avisos:
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
        item = registro.register(scope, key, ficha["kind"], ref_id, ficha["etiqueta"], ficha["datos"], ficha,
                                 actor=getpass.getuser(), source_file=ficha["path"].name, for_type=args.type)
        indice = {"INDEXED": "indexada", "PENDING": "SIN INDEXAR (corre refs:reindex)", "NONE": "sin índice vectorial"}
        print(f"  ok  {item['Sk']}  ->  s3://{registro.bucket}/{item['S3Key']}  [{indice.get(item.get('IndexStatus'), '')}]")
    print(f"\nListo. Desde ahora los documentos de {scope} {key} se comparan contra ellas.")
    return 0


def cmd_register_root(args) -> int:
    """Alta en lote: `--root <carpeta>` con UNA subcarpeta por entidad (su
    nombre es la clave). Cada una pasa por el mismo `cmd_register` — misma
    revisión, mismo plan, misma confirmación (o `--yes`) —, así que repetir la
    corrida es seguro: lo ya registrado sale como YA REGISTRADA y se omite.
    Una carpeta con clave inválida o que falla se reporta y NO detiene al resto."""
    raiz = Path(args.root)
    if not raiz.is_dir():
        raise SystemExit(f"No existe la carpeta: {raiz}")
    carpetas = sorted(c for c in raiz.iterdir() if c.is_dir() and not c.name.startswith("."))
    if not carpetas:
        raise SystemExit(f"{raiz} no tiene subcarpetas (una por entidad).")
    fallidas = []
    for carpeta in carpetas:
        args.key, args.dir = carpeta.name, str(carpeta)
        try:
            cmd_register(args)
        except SystemExit as e:
            fallidas.append((carpeta.name, str(e)))
            print(f"\n  !! {carpeta.name}: {e}")
        except Exception as e:
            fallidas.append((carpeta.name, f"{type(e).__name__}: {e}"))
            print(f"\n  !! {carpeta.name}: {type(e).__name__}: {e}")
    print(f"\n{len(carpetas) - len(fallidas)} de {len(carpetas)} entidades procesadas"
          + (f"; con problemas: {', '.join(n for n, _ in fallidas)}" if fallidas else "."))
    return 1 if fallidas else 0


def _todas(registro) -> List[Dict[str, Any]]:
    from boto3.dynamodb.conditions import Attr

    items, kwargs = [], {"FilterExpression": Attr("Pk").begins_with("R#")}
    while True:
        pagina = registro.table.scan(**kwargs)
        items.extend(pagina.get("Items") or [])
        if not pagina.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = pagina["LastEvaluatedKey"]


def cmd_list(args) -> int:
    from pipeline import reference_registry

    registro, nombre_tabla = _recursos(args, con_bucket=False)
    if args.key:
        scope, _ = resolver_scope(args)
        items = registro.items(scope, clave_valida(args))
    else:
        items = _todas(registro)
    if not items:
        print(f"Sin referencias registradas ({nombre_tabla}).")
        return 0
    actual = None
    for item in sorted(items, key=lambda i: (i["Pk"], i["Sk"])):
        if item["Pk"] != actual:
            actual = item["Pk"]
            _, scope, key = actual.split("#", 2)
            print(f"\n{scope} {key}")
        estado = {"ACTIVE": "activa", "INACTIVE": "INACTIVA", "PENDING": "pendiente"}[reference_registry.status_of(item)]
        indice = {"INDEXED": "idx", "PENDING": "idx?"}.get(item.get("IndexStatus"), "")
        print(f"  {item['Sk']:40s} {estado:9s} {indice:4s} {item.get('Width', '?')}x{str(item.get('Height', '?')):<6} "
              f"{str(item.get('CreatedAt', ''))[:10]}  {item.get('Label', '')}")
    return 0


def cmd_remove(args) -> int:
    from pipeline import reference_registry

    if not (args.key and args.kind and args.ref):
        raise SystemExit("refs:remove necesita --key, --kind y --ref (ver `pnpm refs:list -- --key ...`)")
    scope, elementos = resolver_scope(args)
    if args.kind not in elementos:
        raise SystemExit(f"--kind {args.kind} no es un elemento de {scope}: {', '.join(elementos)}")
    key = clave_valida(args)
    registro, _ = _recursos(args)
    accion = "BORRAR (fila + imagen + vector, irreversible)" if args.delete else "DESACTIVAR"
    print(f"{accion}: {scope} {key} {args.kind}#{args.ref}")
    if not confirmar(args, "¿Continuar?"):
        return 0
    try:
        if args.delete:
            registro.delete(scope, key, args.kind, args.ref)
        else:
            registro.deactivate(scope, key, args.kind, args.ref, actor=getpass.getuser())
    except reference_registry.RegistryError as e:
        raise SystemExit(e.message)
    print("Hecho.")
    return 0


def cmd_reindex(args) -> int:
    """Embeddings e índice vectorial al día con lo que dice la tabla (la fuente
    de verdad): rellena las referencias registradas antes de que existieran y
    reconstruye el índice tras cambiar de modelo. `--force` recalcula todo."""
    from pipeline import reference_registry

    registro, nombre_tabla = _recursos(args)
    if registro.provider is None:
        raise SystemExit("Los embeddings están desactivados (REFERENCE_EMBEDDINGS_ENABLED / proveedor desconocido).")
    if args.key:
        scope, _ = resolver_scope(args)
        items = registro.items(scope, clave_valida(args))
    else:
        items = _todas(registro)
    activas = [i for i in items if reference_registry.status_of(i) == "ACTIVE"]
    al_dia = [i for i in activas if i.get("IndexStatus") == "INDEXED"
              and i.get("EmbeddingModel") == registro.provider.model_id]
    pendientes = activas if args.force else [i for i in activas if i not in al_dia]
    print(f"{nombre_tabla}: {len(activas)} activas, {len(al_dia)} al día con {registro.provider.model_id}; "
          f"se procesarán {len(pendientes)} (~${len(pendientes) * 0.00006:.4f} en embeddings).")
    if not pendientes or not confirmar(args, f"¿Reindexar {len(pendientes)} referencia(s) en {args.env}?"):
        return 0
    cuenta: Dict[str, int] = {}
    for item in pendientes:
        resultado = registro.reindex(item, recompute=args.force)
        cuenta[resultado] = cuenta.get(resultado, 0) + 1
        if resultado == "FAILED":
            print(f"  FALLÓ  {item['Pk']} {item['Sk']}")
    print("Hecho: " + ", ".join(f"{n} {k}" for k, n in sorted(cuenta.items())))
    return 1 if cuenta.get("FAILED") else 0


def cmd_search(args) -> int:
    """¿De quién es este recorte? Lo mismo que hace la etapa cuando la carga no
    declara la clave: embedding + índice vectorial, sin modelo de visión."""
    from pipeline import reference_embeddings, reference_index

    if not (args.file and args.kind):
        raise SystemExit("refs:search necesita --file y --kind")
    scope, elementos = resolver_scope(args)
    if args.kind not in elementos:
        raise SystemExit(f"--kind {args.kind} no es un elemento de {scope}: {', '.join(elementos)}")
    registro, _ = _recursos(args, con_bucket=False)
    indice = reference_index.default_index(registro.provider)
    if registro.provider is None or indice is None:
        raise SystemExit("Este ambiente no tiene índice vectorial (¿falta desplegar?).")
    vector, _ = reference_embeddings.embed(Path(args.file).read_bytes(), registro.provider)
    if vector is None:
        raise SystemExit("No se pudo calcular el embedding (¿acceso al modelo en Bedrock?).")
    hallazgos = indice.search(vector, scope=scope, kind=args.kind, top_k=args.top, model=registro.provider.model_id)
    if hallazgos is None:
        raise SystemExit("La búsqueda falló (ver el log).")
    if not hallazgos:
        print("Sin resultados.")
    for h in hallazgos:
        print(f"  {h['similarity']:.3f}  {scope} {h['key']}  {args.kind}#{h['refId']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["register", "list", "remove", "reindex", "search"])
    parser.add_argument("--key", help="Clave de la entidad (p. ej. el número de proveedor que el upload manda en metadata).")
    parser.add_argument("--dir", help="Carpeta con los recortes clasificados (register).")
    parser.add_argument("--root", help="register en lote: carpeta con una subcarpeta por entidad (nombre = clave).")
    parser.add_argument("--file", help="Un solo recorte (register/search); requiere --kind.")
    parser.add_argument("--kind", help="Elemento: FIRMA, SELLO, LOGO... (los del catálogo).")
    parser.add_argument("--label", help="Etiqueta legible con --file (quién firma, qué sucursal).")
    parser.add_argument("--ref", help="refId a dar de baja (remove).")
    parser.add_argument("--delete", action="store_true", help="remove: borra en vez de desactivar.")
    parser.add_argument("--force", action="store_true",
                        help="register: sube aunque ya haya una con la misma tinta. reindex: recalcula todo.")
    parser.add_argument("--top", type=int, default=5, help="search: cuántos resultados.")
    parser.add_argument("--type", default="CARTA_NOM35", help="Tipo del que se toma el scope por defecto.")
    parser.add_argument("--scope", help="Scope explícito (p. ej. PROVEEDOR); gana sobre --type.")
    parser.add_argument("--sin-embeddings", action="store_true", dest="sin_embeddings",
                        help="No calcular embeddings ni tocar el índice vectorial.")
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
    # pnpm corre el script desde la raíz del repo, no desde donde lo invocaste:
    # las rutas relativas se resuelven contra TU carpeta (INIT_CWD la trae pnpm).
    origen = __import__("os").environ.get("INIT_CWD")
    for nombre in ("dir", "root", "file"):
        valor = getattr(args, nombre)
        if valor and origen and not Path(valor).is_absolute() and not Path(valor).exists():
            setattr(args, nombre, str(Path(origen) / valor))
    if args.command == "register" and args.root:
        if args.key or args.dir or args.file:
            parser.error("--root no se combina con --key/--dir/--file")
        return cmd_register_root(args)
    if args.command == "register" and not args.key:
        parser.error("register necesita --key (o --root para varias entidades)")
    if args.command == "register" and not (args.dir or args.file):
        parser.error("register necesita --dir o --file")
    return {"register": cmd_register, "list": cmd_list, "remove": cmd_remove,
            "reindex": cmd_reindex, "search": cmd_search}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
