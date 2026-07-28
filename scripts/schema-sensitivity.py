#!/usr/bin/env python3
"""Experimento: ¿qué tan sensible es la extracción/validación de
PEDIMENTO_ADUANAL a cambios pequeños en el catálogo?

Corre UNA extracción real (baseline, Bedrock+Textract) contra un documento
de prueba y luego aplica mutaciones al catálogo — quitar/agregar hints,
required_fields, tolerancias — midiendo el efecto contra el ground truth de
tests/integration/expected_pedimentos.json.

Dos tipos de mutación, con costo MUY distinto (por diseño, para no gastar de
más): la extracción (llamada a Bedrock) es cara; la validación (motor de
reglas sobre datos ya extraídos) es gratis y local.

- VALIDATION-ONLY (gratis, instantáneo): tocan validation_rules,
  required_fields o min_confidence. No cambian lo que se le pide al modelo
  (el prompt/schema de extracción es el mismo), así que se re-evalúan sobre
  la MISMA extracción baseline, sin llamar a Bedrock otra vez.
- EXTRACTION-AFFECTING (paga ~lo mismo que un documento normal, porque el
  prompt/tool-schema real que ve el modelo cambia): tocan extraction_hints
  o extraction_fields. Cada una dispara una llamada nueva a Bedrock.

Uso:
    .venv/bin/python scripts/schema-sensitivity.py --list
    .venv/bin/python scripts/schema-sensitivity.py --file normal_pend.pdf
    .venv/bin/python scripts/schema-sensitivity.py --file normal_pend.pdf --skip-paid
    .venv/bin/python scripts/schema-sensitivity.py --file normal_pend.pdf --only tighten_tolerance_incrementables
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
LAMBDA_DIR = REPO / "src" / "document-processing" / "resources" / "enrichment-lambda"
sys.path.insert(0, str(LAMBDA_DIR))

DOCS_DIR = REPO / "test-docs" / "pedimentos docs" / "docs"
EXPECTED_FILE = REPO / "tests" / "integration" / "expected_pedimentos.json"
TYPE_ID = "PEDIMENTO_ADUANAL"

GREEN, RED, YELLOW, CYAN, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m"

import document_catalog as dc  # noqa: E402
from document_catalog import TypeRegistryEntry  # noqa: E402
from validators import engine  # noqa: E402
from validators.engine import ValidationContext  # noqa: E402

# Reutiliza el matcher de valores del runner existente en vez de duplicarlo.
import importlib.util  # noqa: E402


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tp = _load_module("test_pedimentos_sens", REPO / "scripts" / "test-pedimentos.py")


# --- Mutation helpers (pure: TypeRegistryEntry -> TypeRegistryEntry) --------

def _set_rule_param(entry: TypeRegistryEntry, field: str, rule_type: str, **updates) -> TypeRegistryEntry:
    new_rules = []
    for r in entry.validation_rules:
        if r.field == field and r.rule_type == rule_type:
            r = dataclasses.replace(r, params={**r.params, **updates})
        new_rules.append(r)
    return dataclasses.replace(entry, validation_rules=new_rules)


def _without_required(entry: TypeRegistryEntry, field: str) -> TypeRegistryEntry:
    return dataclasses.replace(entry, required_fields=[f for f in entry.required_fields if f != field])


def _with_min_confidence(entry: TypeRegistryEntry, value: float) -> TypeRegistryEntry:
    return dataclasses.replace(entry, min_confidence=value)


def _with_hint(entry: TypeRegistryEntry, field: str, text: Optional[str]) -> TypeRegistryEntry:
    hints = dict(entry.extraction_hints)
    if text is None:
        hints.pop(field, None)
    else:
        hints[field] = text
    return dataclasses.replace(entry, extraction_hints=hints)


@dataclass
class Mutation:
    name: str
    kind: str  # "validation" | "extraction"
    description: str
    apply: Callable[[TypeRegistryEntry], TypeRegistryEntry]


MUTATIONS: List[Mutation] = [
    Mutation(
        "tighten_tolerance_incrementables", "validation",
        "TOTAL_INCREMENTABLES: tolerance 0.02 -> 0.0001 (casi cero). "
        "¿Cuántos documentos que hoy pasan empiezan a fallar por redondeo?",
        lambda e: _set_rule_param(e, "TOTAL_INCREMENTABLES", "custom_formula", tolerance=0.0001),
    ),
    Mutation(
        "loosen_tolerance_valor_aduana", "validation",
        "VALOR_ADUANA (custom_formula vs VALOR_COMERCIAL_PESOS+incrementables-decrementables): "
        "tolerance 5 -> 20000. Si esto vuelve VÁLIDO un check que hoy es inválido, "
        "significa que una tolerancia floja puede esconder el bug real en vez de detectarlo.",
        lambda e: _set_rule_param(e, "VALOR_ADUANA", "custom_formula", tolerance=20000),
    ),
    Mutation(
        "drop_anio_pedimento_required", "validation",
        "Quita ANIO_PEDIMENTO de required_fields. ¿Cambia el status del documento "
        "aunque el dato extraído sea idéntico?",
        lambda e: _without_required(e, "ANIO_PEDIMENTO"),
    ),
    Mutation(
        "raise_min_confidence", "validation",
        "min_confidence 0.80 -> 0.97. ¿Cuántos campos VALIDATED pasan a LOW_CONFIDENCE "
        "sin que el dato extraído cambie?",
        lambda e: _with_min_confidence(e, 0.97),
    ),
    Mutation(
        "disambiguate_valor_comercial_hint", "extraction",
        "Fortalece el hint de VALOR_COMERCIAL_PESOS explicando que es DISTINTO de "
        "VALOR_ADUANA (bug conocido: el modelo copia el valor de VALOR_ADUANA). "
        "Intento real de arreglo, no solo experimento.",
        lambda e: _with_hint(
            e, "VALOR_COMERCIAL_PESOS",
            "Campo PRECIO PAGADO/VALOR COMERCIAL del encabezado, normalmente en MXN. "
            "Es un campo DISTINTO de VALOR_ADUANA aunque estén impresos cerca uno del otro: "
            "VALOR_ADUANA = este valor + TOTAL_INCREMENTABLES - TOTAL_DECREMENTABLES, así que "
            "casi siempre son números diferentes (VALOR_ADUANA normalmente es mayor). "
            "Nunca copiar aquí el valor impreso en el renglón de VALOR ADUANA.",
        ),
    ),
    Mutation(
        "remove_anio_pedimento_hint", "extraction",
        "Quita el hint de ANIO_PEDIMENTO que dice 'solo los últimos 2 dígitos, no 2026'. "
        "¿Vuelve el bug que ya arreglamos (año en 4 dígitos)?",
        lambda e: _with_hint(e, "ANIO_PEDIMENTO", None),
    ),
]


def _print_list() -> None:
    print(f"{CYAN}Mutaciones disponibles:{RESET}\n")
    for m in MUTATIONS:
        cost = f"{YELLOW}paga (~$0.05, llama a Bedrock){RESET}" if m.kind == "extraction" else f"{GREEN}gratis{RESET}"
        print(f"  {m.name}  [{cost}]")
        print(f"      {m.description}\n")


def _evaluate(entity_map: Dict[str, Any], confidences: Dict[str, float], entry: TypeRegistryEntry,
              expected_fields: Dict[str, Any], expected_values: Dict[str, str]) -> Dict[str, Any]:
    ctx = ValidationContext(expected_values=expected_values, sibling_documents=[], document_text="")
    checks = engine.run_rules(entity_map, entry, ctx)
    resolved = engine.resolve_fields(entity_map, confidences, checks, entry)
    status = engine.document_status(resolved, checks, entry)

    gt_pass = 0
    for fname, fexp in expected_fields.items():
        ok, _note = tp._match_field(resolved, entity_map, fname, fexp)
        gt_pass += int(ok)

    failing = [c for c in checks if not c.valid and not c.skipped]
    return {
        "status": status,
        "gt_pass": gt_pass,
        "gt_total": len(expected_fields),
        "failing_checks": sorted(f"{c.rule}:{c.field}" for c in failing),
        "failing_messages": {f"{c.rule}:{c.field}": c.message for c in failing},
        "field_statuses": {name: f["status"] for name, f in resolved.items()},
    }


def _print_score(label: str, score: Dict[str, Any], kind: Optional[str] = None) -> None:
    tag = f" [{kind}]" if kind else ""
    print(f"  {CYAN}{label}{tag}{RESET}: status={score['status']}  "
          f"ground-truth={score['gt_pass']}/{score['gt_total']}  "
          f"failing_checks={len(score['failing_checks'])}")
    for key in score["failing_checks"]:
        print(f"      {DIM}{key}: {score['failing_messages'].get(key, '')}{RESET}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default="normal_pend.pdf")
    parser.add_argument("--docs", default=str(DOCS_DIR))
    parser.add_argument("--skip-paid", action="store_true", help="solo corre las mutaciones gratis (validación)")
    parser.add_argument("--only", help="corre solo esta mutación (ver --list)")
    parser.add_argument("--list", action="store_true", help="lista las mutaciones disponibles y sale")
    parser.add_argument("--model", help="Bedrock model id override")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    if args.list:
        _print_list()
        return 0

    pdf_path = Path(args.docs) / args.file
    if not pdf_path.exists():
        print(f"{RED}No existe {pdf_path}{RESET}")
        return 2

    expected = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))
    spec = expected.get("files", {}).get(args.file)
    if not spec:
        print(f"{RED}No hay ground truth para '{args.file}'{RESET}")
        return 2
    exp_doc_spec = next((d for d in spec.get("expect_documents", []) if TYPE_ID in d.get("type", [])), None)
    if not exp_doc_spec:
        print(f"{RED}El ground truth de '{args.file}' no tiene una expectativa de tipo {TYPE_ID}{RESET}")
        return 2
    expected_fields = exp_doc_spec.get("fields", {})
    expected_values = {"IMPORTER_RFC": expected.get("importer_rfc")} if expected.get("importer_rfc") else {}

    from pipeline.expediente import process_pdf

    dc.reload_catalog()
    pdf_bytes = pdf_path.read_bytes()

    print(f"{CYAN}=== Sensibilidad del schema: {args.file} ({TYPE_ID}) ==={RESET}\n")
    print(f"{DIM}Corriendo extracción BASELINE (real, Bedrock+Textract, ~$0.05)...{RESET}")
    baseline_result = process_pdf(pdf_bytes, declared_type=TYPE_ID, expected_values=expected_values,
                                   model_id=args.model, source_name=args.file)
    baseline_doc = next((d for d in baseline_result["documents"] if d["documentType"] == TYPE_ID), None)
    if baseline_doc is None:
        print(f"{RED}El baseline no clasificó el documento como {TYPE_ID}: "
              f"{[d['documentType'] for d in baseline_result['documents']]}{RESET}")
        return 1

    entity_map = baseline_doc["extractedEntities"]
    confidences = {name: f["confidence"] for name, f in baseline_doc["fields"].items() if f.get("confidence") is not None}
    base_entry = dc.get_type_entry(TYPE_ID)

    baseline_score = _evaluate(entity_map, confidences, base_entry, expected_fields, expected_values)
    print()
    _print_score("baseline (schema sin cambios)", baseline_score)

    results: List[tuple] = [("baseline", None, baseline_score)]

    print(f"\n{DIM}--- Mutaciones ---{RESET}")
    for m in MUTATIONS:
        if args.only and m.name != args.only:
            continue
        if m.kind == "extraction" and args.skip_paid:
            print(f"\n  {DIM}(saltada: {m.name} — usa --skip-paid){RESET}")
            continue

        mutated_entry = m.apply(base_entry)
        print(f"\n  {YELLOW if m.kind == 'extraction' else GREEN}{m.name}{RESET}  {DIM}({m.kind}){RESET}")
        print(f"      {m.description}")

        if m.kind == "validation":
            score = _evaluate(entity_map, confidences, mutated_entry, expected_fields, expected_values)
        else:
            print(f"      {DIM}llamando a Bedrock con el schema mutado (~$0.05)...{RESET}")
            dc._load_catalog()  # asegura que el cache esté poblado antes de sobreescribir
            dc._CATALOG_CACHE[TYPE_ID] = mutated_entry
            try:
                r = process_pdf(pdf_bytes, declared_type=TYPE_ID, expected_values=expected_values,
                                 model_id=args.model, source_name=args.file)
            finally:
                dc.reload_catalog()  # restaura el baseline para la siguiente mutación
            doc = next((d for d in r["documents"] if d["documentType"] == TYPE_ID), None)
            if doc is None:
                print(f"      {RED}el documento mutado no se clasificó como {TYPE_ID}{RESET}")
                continue
            mut_entities = doc["extractedEntities"]
            mut_conf = {name: f["confidence"] for name, f in doc["fields"].items() if f.get("confidence") is not None}
            score = _evaluate(mut_entities, mut_conf, mutated_entry, expected_fields, expected_values)

        _print_score("  resultado", score, m.kind)
        results.append((m.name, m, score))

    # --- Conclusiones (generadas de los números reales de esta corrida) ----
    print(f"\n{CYAN}=== Conclusiones ==={RESET}")
    for name, m, score in results[1:]:
        delta_gt = score["gt_pass"] - baseline_score["gt_pass"]
        new_fails = sorted(set(score["failing_checks"]) - set(baseline_score["failing_checks"]))
        fixed = sorted(set(baseline_score["failing_checks"]) - set(score["failing_checks"]))
        status_changed = score["status"] != baseline_score["status"]

        bits = []
        if delta_gt > 0:
            bits.append(f"MEJORÓ ground-truth en +{delta_gt} campo(s)")
        elif delta_gt < 0:
            bits.append(f"EMPEORÓ ground-truth en {delta_gt} campo(s)")
        else:
            bits.append("sin cambio en ground-truth")
        if new_fails:
            bits.append(f"rompió {len(new_fails)} check(s) nuevo(s): {', '.join(new_fails)}")
        if fixed:
            bits.append(f"arregló {len(fixed)} check(s): {', '.join(fixed)}")
        if status_changed:
            bits.append(f"status del documento cambió: {baseline_score['status']} -> {score['status']}")

        print(f"  - {name}: " + "; ".join(bits))

    print(f"\n{DIM}Interpretación: las mutaciones 'validation' cambian solo cómo se JUZGA un dato ya\n"
          f"extraído (pueden esconder o exponer bugs reales sin costar nada); las 'extraction'\n"
          f"cambian lo que el modelo realmente ve y puede arreglar o romper la extracción en sí.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
