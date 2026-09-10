"""`pnpm reporte` sin `--env` junta dev y prod: filas etiquetadas, filtro
"Ambiente" en el HTML y un inventario de infraestructura por stack que se
suma sin convertir `None` (no atribuible) en cero."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import reporte as report  # noqa: E402


def _infra(env: str, floor: float, actual, *, available: bool = True) -> dict:
    return {
        "stack": report.stack_id(env), "environment": env, "region": "us-east-1",
        "fixedMonthlyUsd": floor, "fixedMtdActualUsd": actual, "resourceCount": 3,
        "fixedItems": [{"service": "WAF", "component": "WebACL", "quantity": 1, "unitMonthlyUsd": floor, "monthlyUsd": floor}] if floor else [],
        "usageOnlyItems": [],
        "taggedMtd": {"available": False, "totalUsd": None, "services": [], "estimated": False},
        "manualMtd": {"available": False, "totalScopedUsd": None, "services": []},
        "fixedMonthlyHistory": {
            "available": available, "estimated": False, "start": "2026-01-01", "end": "2026-12-31",
            "actualToDateUsd": actual if available else None, "projectedPeriodUsd": floor * 12,
            "currentRunRateMonthlyUsd": floor, "scope": "ok" if available else "no atribuible",
            "rows": [
                {"month": "2026-08", "status": "closed", "actualUsd": floor if available else None, "estimatedFullMonthUsd": floor, "projectedUsd": floor},
                {"month": "2026-09", "status": "mtd", "actualUsd": actual if available else None, "estimatedFullMonthUsd": floor, "projectedUsd": floor},
                {"month": "2026-10", "status": "future", "actualUsd": None, "estimatedFullMonthUsd": floor, "projectedUsd": floor},
            ],
        },
        "environmentExplanation": f"{env} explicado", "warnings": [f"aviso {env}"],
        "reportPeriod": {"start": "2026-01-01", "end": "2026-12-31"},
    }


def test_single_environment_is_returned_untouched() -> None:
    only = _infra("dev", 0.0, 0.0)
    assert report.merge_infrastructure_snapshots({"dev": only}) is only
    assert report.merge_infrastructure_snapshots({}) == {}


def test_merge_sums_stacks_and_keeps_provenance() -> None:
    merged = report.merge_infrastructure_snapshots({"dev": _infra("dev", 0.0, 0.0), "prod": _infra("prod", 6.0, 2.0)})
    assert merged["environment"] == "dev + prod"
    assert merged["environments"] == ["dev", "prod"]
    assert merged["stack"] == "DocumentValidationStack-Dev + DocumentValidationStack"
    assert merged["fixedMonthlyUsd"] == 6.0
    assert merged["fixedMtdActualUsd"] == 2.0
    assert merged["resourceCount"] == 6
    assert [i["service"] for i in merged["fixedItems"]] == ["[prod] WAF"]
    assert merged["warnings"] == ["[dev] aviso dev", "[prod] aviso prod"]
    assert "dev: dev explicado" in merged["environmentExplanation"]
    rows = {r["month"]: r for r in merged["fixedMonthlyHistory"]["rows"]}
    assert rows["2026-08"]["actualUsd"] == 6.0 and rows["2026-08"]["projectedUsd"] == 6.0
    assert rows["2026-09"]["status"] == "mtd" and rows["2026-09"]["actualUsd"] == 2.0
    assert rows["2026-10"]["actualUsd"] is None  # futuro sigue siendo ausencia, no cero
    assert merged["fixedMonthlyHistory"]["actualToDateUsd"] == 8.0
    assert merged["fixedMonthlyHistory"]["projectedPeriodUsd"] == 72.0  # 6*12 de prod, no la suma


def test_projections_come_from_prod_only_even_if_dev_has_a_floor() -> None:
    # dev con piso 4 (no es lo normal, pero aísla la regla): lo real se suma,
    # la proyección ignora dev por completo.
    merged = report.merge_infrastructure_snapshots({"dev": _infra("dev", 4.0, 1.0), "prod": _infra("prod", 6.0, 2.0)})
    assert merged["projectionEnvironment"] == "prod"
    assert merged["projectionFixedMonthlyUsd"] == 6.0
    assert merged["fixedMonthlyUsd"] == 10.0  # piso real actual de los dos stacks
    hist = merged["fixedMonthlyHistory"]
    rows = {r["month"]: r for r in hist["rows"]}
    assert rows["2026-08"]["actualUsd"] == 10.0          # real: dev + prod
    assert rows["2026-08"]["projectedUsd"] == 6.0         # proyección: solo prod
    assert rows["2026-09"]["estimatedFullMonthUsd"] == 6.0
    assert hist["projectedPeriodUsd"] == 72.0 and hist["currentRunRateMonthlyUsd"] == 6.0
    assert "proyección basada solo en prod" in hist["scope"]
    assert report.projection_environment(["dev"]) == "dev"
    assert report.projection_environment(["dev", "prod"]) == "prod"
    assert report.projection_environment([]) is None


def test_markdown_projection_uses_only_prod_rows() -> None:
    def row(env: str, cost: float, pages: int) -> dict:
        return {"id": f"{env}-{cost}", "environment": env, "type": "X", "typeName": "X", "appOrigin": "", "processedAt": "2026-09-01T00:00:00",
                "hasCost": True, "hasQuality": True, "workflowStatus": "complete", "validationStatus": "APROBADO", "model": "m",
                "processingSeconds": 1.0, "avgConfidence": 0.9, "services": {}, "totalCost": cost, "pagesTotal": pages,
                "totalInputTokens": 0, "totalOutputTokens": 0, "subDocCount": 0, "warnings": [], "fieldStats": {},
                "pageSelection": None, "trafficKind": "operational"}
    by_env = {"dev": _infra("dev", 0.0, 0.0), "prod": _infra("prod", 6.0, 2.0)}
    merged = report.merge_infrastructure_snapshots(by_env)
    # dev: $1/página (carísimo, pruebas); prod: $0.01/página.
    rows = report.hydrate_processing_fields([row("dev", 10.0, 10), row("prod", 0.1, 10)])
    md = report.render_markdown(rows, "dev + prod", None, merged)
    pages = report.COST_REFERENCE["volume"]["pagesPerMonth"]
    expected = 0.01 * pages + 6.0
    assert "**solo prod**" in md
    assert f"**${expected:,.2f}/mes**" in md


def test_merge_does_not_fake_actuals_when_one_stack_is_not_attributable() -> None:
    merged = report.merge_infrastructure_snapshots({
        "dev": _infra("dev", 0.0, None, available=False), "prod": _infra("prod", 6.0, 2.0),
    })
    assert merged["fixedMonthlyHistory"]["available"] is False
    assert merged["fixedMonthlyHistory"]["actualToDateUsd"] is None
    assert all(r["actualUsd"] is None for r in merged["fixedMonthlyHistory"]["rows"])
    assert merged["fixedMtdActualUsd"] == 2.0  # lo que sí se pudo demostrar, con el aviso de dev al lado


def test_join_tags_rows_with_their_environment() -> None:
    cost = [{"document_id": "a", "document_type": "X", "document_type_name": "X", "services": {}, "total_cost": 0.0}]
    rows = report.join_rows(cost, [], environment="prod")
    assert rows[0]["environment"] == "prod"
    assert report.env_of({}) == "(sin ambiente)"
    assert report.environment_label(["dev", "prod"]) == "dev + prod"
    assert report.environment_slug(["dev", "prod"]) == "dev-prod"


def test_html_carries_one_inventory_per_environment_and_the_filter() -> None:
    by_env = {"dev": _infra("dev", 0.0, 0.0), "prod": _infra("prod", 6.0, 2.0)}
    html = report.render_html(
        [], "dev + prod", infrastructure=report.merge_infrastructure_snapshots(by_env),
        catalog_types=["X"], infrastructure_by_env=by_env,
    )
    assert "<h1>Reporte — dev + prod</h1>" in html
    assert 'id="f-envs"' in html and 'id="f-envs-group"' in html
    assert '"environments": ["dev", "prod"]' in html
    assert "function mergeInfra" in html and "function monthlySpend" in html
    assert "['environment','Ambiente']" in html
    # Sin infraestructura por ambiente, el ambiente único sigue funcionando.
    single = report.render_html([], "dev", infrastructure={"fixedMonthlyUsd": 0})
    assert '"environments": ["dev"]' in single


def test_markdown_adds_por_ambiente_only_when_there_are_two() -> None:
    def row(env: str) -> dict:
        return {"id": env, "environment": env, "type": "X", "typeName": "X", "appOrigin": "", "processedAt": "2026-09-01T00:00:00",
                "hasCost": False, "hasQuality": True, "workflowStatus": "complete", "validationStatus": "APROBADO", "model": "m",
                "processingSeconds": 1.0, "avgConfidence": 0.9, "services": {}, "totalCost": None, "pagesTotal": 0,
                "totalInputTokens": None, "totalOutputTokens": None, "subDocCount": 0, "warnings": [], "fieldStats": {},
                "pageSelection": None, "trafficKind": "operational"}
    both = report.render_markdown(report.hydrate_processing_fields([row("dev"), row("prod")]), "dev + prod")
    assert "## Por ambiente" in both and "| prod | 1 |" in both
    one = report.render_markdown(report.hydrate_processing_fields([row("dev")]), "dev")
    assert "## Por ambiente" not in one
