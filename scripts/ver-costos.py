#!/usr/bin/env python3
"""Audita cuánto ha costado REALMENTE procesar los documentos subidos: por
documento, por tipo, y total acumulado — Bedrock (segmentación, extracción,
y los pasos de clasificación/procesamiento del blueprint cuando sí corren),
Textract OCR y cómputo Lambda desglosados y con su costo en USD.

Lee la tabla `document-validation-costs[-dev]`, que `cost_audit.py` llena
automáticamente en cada invocación del enrichment Lambda (uno por documento
subido, sin importar cómo — API, `pnpm enviar`, etc.). Tabla separada de la
de tracking de documentos: esta es para auditoría/finanzas, no para buscar
documentos por campo extraído.

Uso:
    pnpm costos                          # resumen: total + desglose por tipo
    pnpm costos -- --id e07e3e90         # detalle de un documento (prefijo basta)
    pnpm costos -- --env prod
    pnpm costos -- --limit 30            # más filas en "últimos documentos"
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional

GREEN, RED, YELLOW, CYAN, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m"


def table_name(env: str) -> str:
    return "document-validation-costs" + ("" if env == "prod" else "-dev")


def _table(table: str, region: str):
    import boto3  # deferred: only needed once we know we're actually querying

    return boto3.resource("dynamodb", region_name=region).Table(table)


def scan_all(table: str, region: str) -> List[Dict[str, Any]]:
    t = _table(table, region)
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {}
    while True:
        resp = t.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def find_document(table: str, region: str, doc_id: str) -> Optional[Dict[str, Any]]:
    item = _table(table, region).get_item(Key={"DocumentId": doc_id}).get("Item")
    if item:
        return item
    # Fall back to a prefix match over the whole table, same convenience
    # ver-resultado.py offers for Step Functions execution names.
    matches = [i for i in scan_all(table, region) if i.get("DocumentId", "").startswith(doc_id)]
    return matches[0] if matches else None


def money(x: Any) -> str:
    return f"${float(x):,.4f}"


def _service_detail(svc: Dict[str, Any]) -> str:
    if "inputTokens" in svc:
        detail = f"{svc.get('modelId') or '(sin llamada)'} in={svc.get('inputTokens', 0)} out={svc.get('outputTokens', 0)}"
        if svc.get("cacheReadTokens") or svc.get("cacheWriteTokens"):
            detail += f" cache_r={svc.get('cacheReadTokens', 0)} cache_w={svc.get('cacheWriteTokens', 0)}"
        return detail
    if "pages" in svc:
        return f"{svc.get('pages', 0)} páginas OCR"
    if "durationMs" in svc:
        return f"{float(svc.get('durationMs', 0)):.0f}ms @ {svc.get('memoryMb', 0)}MB (aprox.)"
    return ""


def print_detail(item: Dict[str, Any]) -> None:
    print(f"\n{CYAN}{item.get('DocumentId')}{RESET}  {item.get('DocumentTypeName', '')} ({item.get('DocumentType', '')})")
    meta = f"{item.get('SourceFile', '')}  |  procesado {item.get('ProcessedAt', '')}"
    if item.get("AppOrigin"):
        meta += f"  |  app: {item['AppOrigin']}"
    print(f"{DIM}{meta}{RESET}")
    pages = item.get("Pages")
    if pages:
        print(
            f"Páginas: {pages.get('Total')} "
            f"({pages.get('Native', 0)} nativas, {pages.get('Ocr', 0)} OCR, {pages.get('Empty', 0)} vacías)"
        )

    print(f"\n{'Servicio':<28}{'Detalle':<52}{'Costo':>10}")
    print("-" * 90)
    for name, svc in (item.get("Services") or {}).items():
        print(f"{name:<28}{_service_detail(svc):<52}{money(svc.get('costUsd', 0)):>10}")
    print("-" * 90)
    print(f"{'TOTAL':<80}{money(item.get('TotalCostUsd', 0)):>10}")
    if item.get("TotalInputTokens") is not None or item.get("TotalOutputTokens") is not None:
        print(
            f"{DIM}Tokens totales: in={item.get('TotalInputTokens', 0)} "
            f"out={item.get('TotalOutputTokens', 0)}{RESET}"
        )
    if item.get("CostPerPageUsd") is not None:
        print(f"{DIM}Costo por página: {money(item['CostPerPageUsd'])}  (pricingVersion {item.get('PricingVersion', '?')}){RESET}")
    print()


def print_summary(items: List[Dict[str, Any]], limit: int) -> None:
    by_type: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "pages": 0, "cost": Decimal("0")})
    grand_total = Decimal("0")
    grand_pages = 0
    for it in items:
        dtype = str(it.get("DocumentType") or "OTRO")
        cost = Decimal(str(it.get("TotalCostUsd", 0)))
        pages = int((it.get("Pages") or {}).get("Total", 0))
        agg = by_type[dtype]
        agg["count"] += 1
        agg["pages"] += pages
        agg["cost"] += cost
        grand_total += cost
        grand_pages += pages

    print(f"\n{CYAN}Costos de procesamiento — {len(items)} documento(s){RESET}\n")
    print(f"{'Tipo':<36}{'docs':>6}{'páginas':>10}{'costo total':>14}{'$/doc':>10}{'$/página':>12}")
    print("-" * 88)
    for dtype, agg in sorted(by_type.items(), key=lambda kv: kv[1]["cost"], reverse=True):
        per_doc = agg["cost"] / agg["count"] if agg["count"] else Decimal("0")
        per_page = money(agg["cost"] / agg["pages"]) if agg["pages"] else "-"
        print(f"{dtype:<36}{agg['count']:>6}{agg['pages']:>10}{money(agg['cost']):>14}{money(per_doc):>10}{per_page:>12}")
    print("-" * 88)
    overall_per_doc = money(grand_total / len(items)) if items else "-"
    overall_per_page = money(grand_total / grand_pages) if grand_pages else "-"
    print(f"{'TOTAL':<36}{len(items):>6}{grand_pages:>10}{money(grand_total):>14}{overall_per_doc:>10}{overall_per_page:>12}")

    recent = sorted(items, key=lambda i: str(i.get("ProcessedAt", "")), reverse=True)[:limit]
    print(f"\n{DIM}Últimos {len(recent)} documento(s):{RESET}")
    print(f"{'DocumentId':<38}{'Tipo':<26}{'costo':>10}{'páginas':>9}  procesado")
    for it in recent:
        pages = (it.get("Pages") or {}).get("Total", "-")
        print(
            f"{it.get('DocumentId', ''):<38}{str(it.get('DocumentType', ''))[:24]:<26}"
            f"{money(it.get('TotalCostUsd', 0)):>10}{str(pages):>9}  {it.get('ProcessedAt', '')}"
        )
    print(f"\n{DIM}Detalle de un documento: pnpm costos -- --id <DocumentId o prefijo>{RESET}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", help="DocumentId (o prefijo) — muestra el detalle de un documento")
    parser.add_argument("--env", choices=("dev", "prod"), default="dev", help="ambiente a consultar (default: dev)")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--limit", type=int, default=15, help="filas en 'últimos documentos' del resumen (default 15)")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--"])

    table = table_name(args.env)

    try:
        if args.id:
            item = find_document(table, args.region, args.id)
            if item is None:
                print(f"{RED}No encontré ningún documento con id/prefijo '{args.id}' en {table}{RESET}")
                return 1
            print_detail(item)
            return 0

        items = scan_all(table, args.region)
    except Exception as e:  # credenciales, tabla inexistente, etc.
        print(f"{RED}No pude leer la tabla {table} en {args.region}: {e}{RESET}")
        return 1

    if not items:
        print(f"{YELLOW}La tabla {table} está vacía — aún no se ha procesado ningún documento con este stack.{RESET}")
        return 0
    print_summary(items, limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
