#!/usr/bin/env python3
"""Mantiene al día el registro de actividades del proyecto (Excel + .md).

La fuente de verdad es la hoja "Tareas" de
`documentacion/Proyecto Gestor Documental Inteligente.xlsx`; el .md
(`documentacion/ACTIVIDADES_PROYECTO.md`) siempre se regenera desde ese Excel.

Uso (desde la raíz):
  pnpm actividades                  # sync: agrega los commits nuevos y regenera el .md
  pnpm actividades status           # muestra qué commits están pendientes
  pnpm actividades add "Tarea" [--impacto Alto|Medio|Bajo] [--fecha d/m/aaaa]
                       [--distribuible TXT] [--notas TXT] [--hito 3|4]
  pnpm actividades md               # solo regenera el .md
  pnpm actividades hook             # lo que corre el hook post-commit (solo este repo)

Los commits van en inglés; la actividad registrada va en español. Orden de prioridad:
línea `Actividad: <texto>` en el cuerpo del mensaje; entrada `"<hash>": "texto"` (o
{"tarea","impacto"}) en `documentacion/actividades.descripciones.json`; traducción
automática con la CLI de Claude Code (`claude -p`, se omite con ACTIVIDADES_SIN_IA=1);
si nada funciona, el asunto con su prefijo traducido
(feat -> Funcionalidad, fix -> Corrección...). También puedes editar la celda
en el Excel: el siguiente `pnpm actividades md` respeta lo que esté en el Excel.

Un commit se considera registrado cuando su hash aparece en Notas ("Commit abc1234").
El estado (último commit sincronizado por repo) vive en
`documentacion/actividades.estado.json`, así que borrar una fila del Excel no la
vuelve a agregar.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import warnings
import zipfile
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "documentacion")
XLSX = os.path.join(DOCS, "Proyecto Gestor Documental Inteligente.xlsx")
MD = os.path.join(DOCS, "ACTIVIDADES_PROYECTO.md")
STATE = os.path.join(DOCS, "actividades.estado.json")
DESCRIPCIONES = os.path.join(DOCS, "actividades.descripciones.json")
SHEET = "Tareas"
RESPONSABLE = "LUIS HERNANDEZ MACIAS"
HITO = {
    3: "FASE 3: DISCOVERY Y DEFINICIÓN (Primer bloque)",
    4: "FASE 4: CONSTRUCCIÓN Y CONTROL (Segundo bloque)",
}
GITLAB = "https://code.alsuper.com/Infraestructura"

# Repos de los que se registran commits. `paths`/`grep` acotan los commits que
# cuentan como parte del proyecto (se unen); sin ellos entra todo el historial.
REPOS = [
    dict(name="idp", path=ROOT, url=f"{GITLAB}/idp/-/commit/", tag=""),
    dict(
        name="idp-sdk-node",
        path=os.path.join(os.path.dirname(ROOT), "documents-validator-sdk"),
        url=f"{GITLAB}/idp-sdk-node/-/commit/",
        tag=" (repo idp-sdk-node)",
    ),
    dict(
        name="policy-manager",
        path=os.path.expanduser("~/Desktop/All/repos/policy-manager"),
        url=f"{GITLAB}/insurance-policy/-/commit/",
        tag=" (repo insurance-policy / policy-manager)",
        paths=[
            "apps/api/src/document-extraction",
            "apps/api/src/config/idp.config.ts",
            "docs/historia-usuario-lectura-ia-certificados.md",
        ],
        grep="IDP_SDK|idp",
    ),
]
PREFIJOS = {
    "feat": "Funcionalidad", "fix": "Corrección", "docs": "Documentación",
    "refactor": "Refactor", "chore": "Mantenimiento", "test": "Pruebas",
    "perf": "Rendimiento", "ci": "CI/CD", "style": "Estilo", "build": "Build",
}


def git(repo, *args):
    return subprocess.check_output(["git", "-C", repo, *args], text=True, stderr=subprocess.DEVNULL)


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {"ultimo": {}}


def save_state(st):
    json.dump(st, open(STATE, "w"), indent=2, ensure_ascii=False)
    open(STATE, "a").write("\n")


def commits_pendientes(repo, ultimo):
    """Commits del repo posteriores al último sincronizado, del más viejo al más nuevo."""
    path = repo["path"]
    if not os.path.isdir(path):
        return []
    rng = f"{ultimo}..HEAD" if ultimo else "HEAD"
    fmt = "--pretty=%h%x1f%ad%x1f%s%x1f%b%x1e"
    base = ["log", "--reverse", "--date=format:%d/%m/%Y", fmt, rng]
    outs = []
    if repo.get("paths") or repo.get("grep"):
        if repo.get("paths"):
            outs.append(git(path, *base, "--", *repo["paths"]))
        if repo.get("grep"):
            outs.append(git(path, *base, "-i", "-E", f"--grep={repo['grep']}"))
    else:
        outs.append(git(path, *base))
    vistos, res = set(), []
    for out in outs:
        for blk in out.split("\x1e"):
            blk = blk.strip("\n")
            if not blk:
                continue
            h, fecha, asunto, cuerpo = (blk.split("\x1f") + ["", "", "", ""])[:4]
            if h in vistos:
                continue
            vistos.add(h)
            res.append((h, fecha, asunto, cuerpo))
    res.sort(key=lambda r: git(path, "log", "-1", "--format=%ct", r[0]))
    return res


def traducir(asunto, cuerpo):
    """Redacta la actividad en español con la CLI de Claude Code; None si no está disponible."""
    if os.environ.get("ACTIVIDADES_SIN_IA"):
        return None
    prompt = (
        "Redacta en español, en una sola línea de máximo 150 caracteres, la actividad realizada que describe "
        "este commit de git. Sin comillas, sin punto final y sin explicación; empieza con un sustantivo o "
        "infinitivo nominal (p. ej. 'Panel de restauración...'). Conserva tal cual nombres de archivos, "
        "componentes, endpoints y tipos de documento.\n\nAsunto: " + asunto + "\n\n" + cuerpo[:600]
    )
    try:
        out = subprocess.run(["claude", "-p", "--model", "haiku", prompt], capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return None
    linea = out.stdout.strip().splitlines()[0].strip() if out.returncode == 0 and out.stdout.strip() else ""
    return linea or None


def descripcion(asunto, cuerpo, h=""):
    """(texto en español, pendiente_de_traducir)."""
    try:
        ov = json.load(open(DESCRIPCIONES)).get(h[:7])
    except (OSError, ValueError):
        ov = None
    if ov:
        return (ov["tarea"] if isinstance(ov, dict) else ov), False
    m = re.search(r"^Actividad:\s*(.+)$", cuerpo, re.M)
    if m:
        return m.group(1).strip(), False
    es = traducir(asunto, cuerpo)
    if es:
        return es, False
    m = re.match(r"^(\w+)(?:\([^)]*\))?!?:\s*(.+)$", asunto)
    if m and m.group(1).lower() in PREFIJOS:
        resto = m.group(2).strip()
        return f"{PREFIJOS[m.group(1).lower()]}: {resto[:1].upper()}{resto[1:]}", True
    return asunto, True


def _override(h):
    try:
        ov = json.load(open(DESCRIPCIONES)).get(h[:7])
    except (OSError, ValueError):
        return {}
    return ov if isinstance(ov, dict) else {}


def impacto(repo, h):
    total = 0
    for l in git(repo["path"], "show", "--numstat", "--format=", h).splitlines():
        a = l.split("\t")
        if len(a) >= 2:
            total += sum(int(x) for x in a[:2] if x.isdigit())
    return "Alto" if total >= 1500 else "Medio" if total >= 300 else "Bajo"


# ---------------------------------------------------------------- lectura
def leer_filas():
    import openpyxl
    warnings.simplefilter("ignore")
    ws = openpyxl.load_workbook(XLSX)[SHEET]
    filas = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[1] is None:
            continue
        filas.append(r)
    return filas


def hashes_registrados(filas):
    s = set()
    for r in filas:
        for c in (r[11], r[12]):
            if isinstance(c, str):
                s.update(re.findall(r"Commit ([0-9a-f]{7})", c))
    return s


def repo_de(notas):
    for r in REPOS:
        if r["tag"] and r["tag"].strip() in (notas or ""):
            return r
    return REPOS[0]


def fecha_txt(v):
    return f"{v.day}/{v.month}/{v.year}" if hasattr(v, "year") else ("" if v is None else str(v))


def regenerar_md():
    filas = leer_filas()
    enc = ["No.", "Tarea", "Impacto", "Responsable", "Estado", "Fecha de requerimiento",
           "Fecha de inicio de actividad", "Fecha de entrega solicitada",
           "Fecha de finalización Real", "Hito", "Distribuible", "Notas", "Commit"]
    out = [
        "# Actividades del proyecto Gestor Documental Inteligente (IDP)\n",
        "Generado desde la hoja `Tareas` de `Proyecto Gestor Documental Inteligente.xlsx` con "
        "`pnpm actividades` (no editar a mano: edita el Excel y regenera). "
        "Cada commit es una tarea realizada y enlaza a GitLab.\n",
        "| " + " | ".join(enc) + " |",
        "|" + "---|" * len(enc),
    ]
    for r in filas:
        n = r[11] if isinstance(r[11], str) else ""
        m = re.search(r"Commit ([0-9a-f]{7})", n) or (re.fullmatch(r"([0-9a-f]{7})", r[12]) if isinstance(r[12], str) else None)
        commit = f"[{m.group(1)}]({repo_de(n)['url']}{m.group(1)})" if m else ""
        no = int(r[0]) if isinstance(r[0], (int, float)) else (r[0] or "")
        cel = [no, r[1], r[2], r[3], r[4], *(fecha_txt(x) for x in r[5:9]), r[9], r[10], r[11], commit]
        cel = [str(c if c is not None else "").replace("|", "/").replace("\n", " ").strip() for c in cel]
        out.append("| " + " | ".join(cel) + " |")
    open(MD, "w").write("\n".join(out) + "\n")
    return len(filas)


# ---------------------------------------------------------------- escritura
def serial(fecha):
    d, m, y = (int(x) for x in fecha.split("/"))
    return (datetime.date(y, m, d) - datetime.date(1899, 12, 30)).days


def sheet_part(z):
    wb = z.read("xl/workbook.xml").decode("utf8")
    rid = re.search(r'<sheet [^>]*name="%s"[^>]*r:id="(rId\d+)"' % SHEET, wb).group(1)
    rels = z.read("xl/_rels/workbook.xml.rels").decode("utf8")
    tgt = re.search(r'<Relationship [^>]*Id="%s"[^>]*Target="([^"]+)"' % rid, rels) or \
        re.search(r'<Relationship [^>]*Target="([^"]+)"[^>]*Id="%s"' % rid, rels)
    t = tgt.group(1).lstrip("/")
    return t if t.startswith("xl/") else "xl/" + t


def agregar_filas(nuevas):
    """nuevas: dicts con tarea, impacto, fecha(d/m/aaaa), hito(3|4), distribuible, notas, commit(hash|None), url."""
    if not nuevas:
        return
    z = zipfile.ZipFile(XLSX)
    part = sheet_part(z)
    s = z.read(part).decode("utf8")
    filas = leer_filas()
    ultimo_no = max((int(r[0]) for r in filas if isinstance(r[0], (int, float))), default=0)
    import openpyxl
    warnings.simplefilter("ignore")
    ws = openpyxl.load_workbook(XLSX)[SHEET]
    ult_fila = max(i for i, r in enumerate(ws.iter_rows(min_col=2, max_col=2, values_only=True), 1) if r[0] is not None)
    mod = re.search(r'<row r="%d"[ >].*?</row>' % ult_fila, s, re.S).group(0)
    estilos = dict(re.findall(r'<c r="([A-Z]+)%d" s="(\d+)"' % ult_fila, mod))

    def txt(col, n, t):
        st = estilos.get(col, "0")
        if not t:
            return f'<c r="{col}{n}" s="{st}"/>'
        return f'<c r="{col}{n}" s="{st}" t="inlineStr"><is><t xml:space="preserve">{escape(str(t))}</t></is></c>'

    fila = ult_fila
    for d in nuevas:
        fila += 1
        ultimo_no += 1
        fch = serial(d["fecha"])
        celdas = [f'<c r="A{fila}" s="{estilos.get("A", "0")}"><v>{ultimo_no}</v></c>',
                  txt("B", fila, d["tarea"]), txt("C", fila, d["impacto"]),
                  txt("D", fila, RESPONSABLE), txt("E", fila, "Completada")]
        for col in "FGHI":
            celdas.append(f'<c r="{col}{fila}" s="{estilos.get(col, "0")}"><v>{fch}</v></c>')
        celdas += [txt("J", fila, HITO[d.get("hito", 4)]), txt("K", fila, d.get("distribuible", "")),
                   txt("L", fila, d.get("notas", ""))]
        if d.get("commit"):
            celdas.append(f'<c r="M{fila}" s="{estilos.get("M", "0")}" t="str"><f>HYPERLINK("{d["url"]}{d["commit"]}","{d["commit"]}")</f><v>{d["commit"]}</v></c>')
        else:
            celdas.append(txt("M", fila, ""))
        nueva = f'<row r="{fila}" ht="40.5" customHeight="1">' + "".join(celdas) + "</row>"
        s, k = re.subn(r'<row r="%d"[ >].*?</row>' % fila, lambda m: nueva, s, count=1, flags=re.S)
        if not k:
            s = s.replace("</sheetData>", nueva + "</sheetData>", 1)
    tmp = XLSX + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
        for it in z.infolist():
            zo.writestr(it, s.encode("utf8") if it.filename == part else z.read(it.filename))
    z.close()
    os.replace(tmp, XLSX)


def sync(solo=None, desde_inicio=False, dry=False):
    st = load_state()
    registrados = hashes_registrados(leer_filas())
    nuevas, avance = [], {}
    for repo in REPOS:
        if solo and repo["name"] != solo:
            continue
        if not os.path.isdir(repo["path"]):
            print(f"  (omitido {repo['name']}: no existe {repo['path']})")
            continue
        ultimo = None if desde_inicio else st["ultimo"].get(repo["name"])
        if ultimo is None and not desde_inicio:
            avance[repo["name"]] = git(repo["path"], "rev-parse", "--short=7", "HEAD").strip()
            print(f"  {repo['name']}: sin línea base; se fija en {avance[repo['name']]} (usa --desde-inicio para cargar todo)")
            continue
        for h, fecha, asunto, cuerpo in commits_pendientes(repo, ultimo):
            if h[:7] in registrados:
                continue
            ov = _override(h)
            texto, pendiente = descripcion(asunto, cuerpo, h)
            nuevas.append(dict(tarea=texto, impacto=ov.get("impacto") or impacto(repo, h), fecha=fecha,
                               hito=4, notas=f"Commit {h[:7]}{repo['tag']}" + ("; pendiente de traducir al español" if pendiente else ""),
                               commit=h[:7], url=repo["url"]))
        avance[repo["name"]] = git(repo["path"], "rev-parse", "--short=7", "HEAD").strip()
    for d in nuevas:
        print(f"  + {d['fecha']}  {d['commit']}  {d['impacto']:5}  {d['tarea'][:90]}")
    if dry:
        return
    agregar_filas(nuevas)
    st["ultimo"].update(avance)
    save_state(st)
    n = regenerar_md()
    print(f"{len(nuevas)} commit(s) agregados; {n} filas en el .md")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("sync"); p.add_argument("--repo"); p.add_argument("--desde-inicio", action="store_true")
    sub.add_parser("status"); sub.add_parser("md"); sub.add_parser("hook")
    a = sub.add_parser("add"); a.add_argument("tarea")
    a.add_argument("--impacto", default="Medio", choices=["Bajo", "Medio", "Alto"])
    a.add_argument("--fecha", default=datetime.date.today().strftime("%d/%m/%Y"))
    a.add_argument("--hito", type=int, default=4, choices=[3, 4])
    a.add_argument("--distribuible", default=""); a.add_argument("--notas", default="")
    args = ap.parse_args()
    cmd = args.cmd or "sync"
    if cmd == "sync":
        sync(getattr(args, "repo", None), getattr(args, "desde_inicio", False))
    elif cmd == "status":
        sync(dry=True)
    elif cmd == "hook":
        sync(solo="idp")
    elif cmd == "md":
        print(f"{regenerar_md()} filas en el .md")
    elif cmd == "add":
        agregar_filas([dict(tarea=args.tarea, impacto=args.impacto, fecha=args.fecha, hito=args.hito,
                            distribuible=args.distribuible, notas=args.notas)])
        print(f"{regenerar_md()} filas en el .md")


if __name__ == "__main__":
    sys.exit(main())
