"""Inventario y costo de plataforma para ``pnpm reporte``.

El costo por documento de ``cost_audit.py`` sólo puede atribuir Bedrock,
Textract y una aproximación de la Lambda de enriquecimiento. Este módulo
mantiene separado lo que no pertenece a una invocación concreta:

* piso mensual, calculado desde los recursos que CloudFormation confirma que
  están desplegados (WAF y claves KMS administradas por el cliente);
* gasto MTD etiquetado por ``CostStack``, cuando la cuenta pagadora activó esa
  clave de asignación en Cost Explorer;
* fallback manual: cruza los tipos físicos del stack con el gasto por servicio
  de la cuenta. Sólo suma lo que puede atribuir de forma exclusiva; el resto se
  conserva como contexto de cuenta, nunca se hace pasar por costo del stack;
* recursos sin renta fija (S3, DynamoDB, API Gateway/API keys), que se cobran
  por consumo y por tanto no deben inventarse como un reparto por documento.

Las tarifas fijas son las públicas de AWS verificadas el 2026-09-08. Cost
Explorer se usa además como comprobación del cargo fijo prorrateado cuando los
recursos WAF/KMS del stack son los únicos facturables de su clase en la cuenta.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence


PRICING_AS_OF = "2026-09-08"
PRICE_SOURCES = {
    "waf": "https://aws.amazon.com/waf/pricing/",
    "kms": "https://aws.amazon.com/kms/pricing/",
}
WAF_ACL_MONTHLY_USD = 5.0
WAF_RULE_MONTHLY_USD = 1.0
KMS_KEY_MONTHLY_USD = 1.0

# Cost Explorer no conoce CloudFormation si la etiqueta de asignación no está
# activa. Este mapa permite hacer el cruce contrario: primero se demuestra que
# el servicio existe en el stack y después se muestra su cargo de cuenta como
# contexto. ``scopedMtdUsd`` sólo se llena cuando además hay evidencia de
# exclusividad (hoy WAF y almacenamiento de CMK).
SERVICE_RESOURCE_TYPES = {
    "AWS WAF": ("AWS::WAFv2::WebACL",),
    "AWS Key Management Service": ("AWS::KMS::Key",),
    "Amazon Simple Storage Service": ("AWS::S3::Bucket",),
    "Amazon DynamoDB": ("AWS::DynamoDB::Table",),
    "Amazon API Gateway": ("AWS::ApiGateway::RestApi",),
    "AWS Lambda": ("AWS::Lambda::Function",),
    "AWS Step Functions": ("AWS::StepFunctions::StateMachine",),
    "Amazon Simple Queue Service": ("AWS::SQS::Queue",),
    "AmazonCloudWatch": ("AWS::Logs::LogGroup", "AWS::CloudWatch::Alarm"),
    "CloudWatch Events": ("AWS::Events::EventBus", "AWS::Events::Rule"),
    "Amazon Simple Notification Service": ("AWS::SNS::Topic",),
}


def _pages(client: Any, operation: str, result_key: str, **kwargs: Any) -> Iterable[Any]:
    """Yield a boto3 list operation whether or not it exposes a paginator."""
    try:
        paginator = client.get_paginator(operation)
    except Exception:
        paginator = None
    if paginator is not None:
        for response in paginator.paginate(**kwargs):
            yield from response.get(result_key, [])
        return
    response = getattr(client, operation)(**kwargs)
    yield from response.get(result_key, [])


def _stack_resources(cfn: Any, stack_name: str) -> List[Dict[str, Any]]:
    return list(_pages(cfn, "list_stack_resources", "StackResourceSummaries", StackName=stack_name))


def _resources_of(resources: Sequence[Dict[str, Any]], resource_type: str) -> List[Dict[str, Any]]:
    return [r for r in resources if r.get("ResourceType") == resource_type]


def _waf_identity(physical_id: str) -> Optional[Dict[str, str]]:
    # CloudFormation serializa WebACL como ``name|uuid|REGIONAL``.
    parts = (physical_id or "").split("|")
    if len(parts) != 3:
        return None
    return {"name": parts[0], "id": parts[1], "scope": parts[2]}


def _all_waf_arns(waf: Any, cloudfront_waf: Any) -> set[str]:
    arns = {
        item.get("ARN", "")
        for item in _pages(waf, "list_web_acls", "WebACLs", Scope="REGIONAL")
        if item.get("ARN")
    }
    arns.update(
        item.get("ARN", "")
        for item in _pages(cloudfront_waf, "list_web_acls", "WebACLs", Scope="CLOUDFRONT")
        if item.get("ARN")
    )
    return arns


def _enabled_customer_keys(kms: Any) -> set[str]:
    keys: set[str] = set()
    for item in _pages(kms, "list_keys", "Keys"):
        key_id = item.get("KeyId")
        if not key_id:
            continue
        try:
            meta = kms.describe_key(KeyId=key_id).get("KeyMetadata") or {}
        except Exception:
            continue
        if meta.get("KeyManager") == "CUSTOMER" and meta.get("KeyState") == "Enabled":
            keys.add(meta.get("KeyId") or key_id)
    return keys


def _ce_usage_rows(ce: Any, start: str, end: str) -> tuple[List[Dict[str, Any]], bool]:
    response = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost", "UsageQuantity"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": ["AWS WAF", "AWS Key Management Service"]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    )
    rows: List[Dict[str, Any]] = []
    estimated = False
    for period in response.get("ResultsByTime", []):
        estimated = estimated or bool(period.get("Estimated"))
        for group in period.get("Groups", []):
            metrics = group.get("Metrics") or {}
            rows.append({
                "periodStart": (period.get("TimePeriod") or {}).get("Start") or start,
                "usageType": (group.get("Keys") or [""])[0],
                "cost": float((metrics.get("UnblendedCost") or {}).get("Amount") or 0),
                "quantity": float((metrics.get("UsageQuantity") or {}).get("Amount") or 0),
                "unit": (metrics.get("UsageQuantity") or {}).get("Unit") or "",
            })
    return rows, estimated


def _next_month(value: date) -> date:
    return date(value.year + (1 if value.month == 12 else 0), 1 if value.month == 12 else value.month + 1, 1)


def _fixed_cost_history(
    ce: Any,
    *,
    period_start: date,
    period_end: date,
    today: date,
    fixed_monthly_usd: float,
    has_waf: bool,
    waf_exclusive: bool,
    has_kms: bool,
    kms_exclusive: bool,
) -> Dict[str, Any]:
    """Reconstruct fixed actuals and a current-footprint projection by month."""
    months: List[date] = []
    cursor = period_start.replace(day=1)
    last = period_end.replace(day=1)
    while cursor <= last:
        months.append(cursor)
        cursor = _next_month(cursor)

    actual_end = min(period_end + timedelta(days=1), today + timedelta(days=1))
    rows: List[Dict[str, Any]] = []
    estimated = False
    if period_start < actual_end and ((not has_waf or waf_exclusive) and (not has_kms or kms_exclusive)):
        rows, estimated = _ce_usage_rows(ce, period_start.isoformat(), actual_end.isoformat())

    actual_by_month: Dict[str, float] = {month.strftime("%Y-%m"): 0.0 for month in months}
    fully_scoped = (not has_waf or waf_exclusive) and (not has_kms or kms_exclusive)
    if fully_scoped:
        for row in rows:
            usage_type = row["usageType"]
            include = (
                (has_waf and (usage_type.endswith("RuleV2") or usage_type.endswith("WebACLV2")))
                or (has_kms and usage_type.endswith("KMS-Keys"))
            )
            if include:
                month = str(row["periodStart"])[:7]
                actual_by_month[month] = actual_by_month.get(month, 0.0) + row["cost"]

    current_month = today.strftime("%Y-%m")
    history_rows: List[Dict[str, Any]] = []
    for month_date in months:
        month = month_date.strftime("%Y-%m")
        if month > current_month:
            status = "future"
            actual: Optional[float] = None
        elif month == current_month:
            status = "mtd"
            actual = round(actual_by_month.get(month, 0.0), 12) if fully_scoped else None
        else:
            status = "closed"
            actual = round(actual_by_month.get(month, 0.0), 12) if fully_scoped else None
        projection = actual if status == "closed" and actual is not None else fixed_monthly_usd
        history_rows.append({
            "month": month,
            "status": status,
            "actualUsd": actual,
            "estimatedFullMonthUsd": fixed_monthly_usd,
            "projectedUsd": projection,
        })

    actual_values = [row["actualUsd"] for row in history_rows if row["actualUsd"] is not None]
    projected_values = [row["projectedUsd"] for row in history_rows if row["projectedUsd"] is not None]
    return {
        "start": period_start.isoformat(),
        "end": period_end.isoformat(),
        "available": fully_scoped,
        "estimated": estimated,
        "actualToDateUsd": round(sum(actual_values), 12) if fully_scoped else None,
        "projectedPeriodUsd": round(sum(projected_values), 12) if projected_values else 0.0,
        "currentRunRateMonthlyUsd": fixed_monthly_usd,
        "rows": history_rows,
        "scope": (
            "Cost Explorer por tipo de uso; exclusividad actual del inventario comprobada"
            if fully_scoped else "No atribuible: existen recursos facturables ajenos al stack"
        ),
    }


def _tagged_cost(ce: Any, stack_name: str, start: str, end: str) -> Dict[str, Any]:
    response = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={"Tags": {"Key": "CostStack", "Values": [stack_name], "MatchOptions": ["EQUALS"]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    services: List[Dict[str, Any]] = []
    estimated = False
    for period in response.get("ResultsByTime", []):
        estimated = estimated or bool(period.get("Estimated"))
        for group in period.get("Groups", []):
            cost = float((((group.get("Metrics") or {}).get("UnblendedCost") or {}).get("Amount")) or 0)
            if cost:
                services.append({"service": (group.get("Keys") or [""])[0], "cost": cost})
    services.sort(key=lambda row: row["cost"], reverse=True)
    return {"available": bool(services), "totalUsd": sum(row["cost"] for row in services),
            "services": services, "estimated": estimated}


def _account_service_costs(ce: Any, start: str, end: str) -> Dict[str, Any]:
    """Read real MTD charges for infra service families, before attribution.

    These values are account-wide. They are useful evidence when ``CostStack``
    is unavailable, but are deliberately not called stack costs here.
    """
    response = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": list(SERVICE_RESOURCE_TYPES)}},
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    costs = {service: 0.0 for service in SERVICE_RESOURCE_TYPES}
    estimated = False
    for period in response.get("ResultsByTime", []):
        estimated = estimated or bool(period.get("Estimated"))
        for group in period.get("Groups", []):
            service = (group.get("Keys") or [""])[0]
            if service not in costs:
                continue
            costs[service] += float(
                ((((group.get("Metrics") or {}).get("UnblendedCost") or {}).get("Amount")) or 0)
            )
    return {"costs": costs, "estimated": estimated}


def _count(resources: Sequence[Dict[str, Any]], resource_type: str) -> int:
    return len(_resources_of(resources, resource_type))


def empty_infrastructure_snapshot(stack_name: str, region: str, message: str) -> Dict[str, Any]:
    return {
        "stack": stack_name,
        "region": region,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "pricingAsOf": PRICING_AS_OF,
        "priceSources": PRICE_SOURCES,
        "fixedMonthlyUsd": None,
        "fixedMtdActualUsd": None,
        "fixedMonthlyHistory": {"available": False, "actualToDateUsd": None, "projectedPeriodUsd": None, "rows": []},
        "fixedItems": [],
        "usageOnlyItems": [],
        "taggedMtd": {"available": False, "totalUsd": None, "services": [], "estimated": False},
        "manualMtd": {"available": False, "totalScopedUsd": None, "services": []},
        "environmentExplanation": message,
        "warnings": [message],
    }


def collect_infrastructure_costs(
    stack_name: str,
    region: str,
    *,
    environment: str,
    now: Optional[date] = None,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    clients: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a JSON-serializable, stack-scoped infrastructure snapshot.

    Failing Cost Explorer never prevents the inventory/fixed estimate from
    being returned. Failing CloudFormation does: without its physical resource
    IDs there is no honest scope boundary.
    """
    if clients is None:
        import boto3

        clients = {
            "cfn": boto3.client("cloudformation", region_name=region),
            "waf": boto3.client("wafv2", region_name=region),
            # CLOUDFRONT-scope WAF is always managed through us-east-1.
            "cloudfront_waf": boto3.client("wafv2", region_name="us-east-1"),
            "kms": boto3.client("kms", region_name=region),
            "ce": boto3.client("ce", region_name="us-east-1"),
        }

    captured = datetime.now(timezone.utc)
    today = now or captured.date()
    period_start = period_start or date(today.year, 1, 1)
    period_end = period_end or date(today.year, 12, 31)
    if period_end < period_start:
        raise ValueError("period_end must be on or after period_start")
    start = today.replace(day=1).isoformat()
    end = (today + timedelta(days=1)).isoformat()  # Cost Explorer end is exclusive.
    warnings: List[str] = []
    resources = _stack_resources(clients["cfn"], stack_name)

    fixed_items: List[Dict[str, Any]] = []
    stack_waf_arns: set[str] = set()
    waf_rules = 0
    for resource in _resources_of(resources, "AWS::WAFv2::WebACL"):
        identity = _waf_identity(resource.get("PhysicalResourceId") or "")
        if not identity:
            warnings.append(f"No se pudo interpretar el Web ACL {resource.get('PhysicalResourceId', '')}.")
            continue
        response = clients["waf"].get_web_acl(Name=identity["name"], Id=identity["id"], Scope=identity["scope"])
        acl = response.get("WebACL") or {}
        if acl.get("ARN"):
            stack_waf_arns.add(acl["ARN"])
        waf_rules += len(acl.get("Rules") or [])

    if stack_waf_arns:
        fixed_items.append({
            "service": "AWS WAF", "component": "Web ACL", "quantity": len(stack_waf_arns),
            "unitMonthlyUsd": WAF_ACL_MONTHLY_USD,
            "monthlyUsd": len(stack_waf_arns) * WAF_ACL_MONTHLY_USD,
            "source": PRICE_SOURCES["waf"],
        })
    if waf_rules:
        fixed_items.append({
            "service": "AWS WAF", "component": "Reglas / managed rule groups", "quantity": waf_rules,
            "unitMonthlyUsd": WAF_RULE_MONTHLY_USD,
            "monthlyUsd": waf_rules * WAF_RULE_MONTHLY_USD,
            "source": PRICE_SOURCES["waf"],
        })

    stack_key_ids: set[str] = set()
    for resource in _resources_of(resources, "AWS::KMS::Key"):
        key_id = resource.get("PhysicalResourceId")
        if not key_id:
            continue
        meta = (clients["kms"].describe_key(KeyId=key_id).get("KeyMetadata") or {})
        if meta.get("KeyManager") == "CUSTOMER" and meta.get("KeyState") == "Enabled":
            stack_key_ids.add(meta.get("KeyId") or key_id)
    if stack_key_ids:
        fixed_items.append({
            "service": "AWS KMS", "component": "Clave administrada por el cliente", "quantity": len(stack_key_ids),
            "unitMonthlyUsd": KMS_KEY_MONTHLY_USD,
            "monthlyUsd": len(stack_key_ids) * KMS_KEY_MONTHLY_USD,
            "source": PRICE_SOURCES["kms"],
        })

    usage_only = [
        {"service": "Amazon S3", "resources": _count(resources, "AWS::S3::Bucket"),
         "fixedMonthlyUsd": 0.0, "billing": "almacenamiento, requests y transferencia"},
        {"service": "Amazon DynamoDB", "resources": _count(resources, "AWS::DynamoDB::Table"),
         "fixedMonthlyUsd": 0.0, "billing": "PAY_PER_REQUEST + almacenamiento"},
        {"service": "Amazon API Gateway", "resources": _count(resources, "AWS::ApiGateway::RestApi"),
         "fixedMonthlyUsd": 0.0, "billing": "requests; API keys y usage plans no tienen renta"},
        {"service": "AWS Lambda", "resources": _count(resources, "AWS::Lambda::Function"),
         "fixedMonthlyUsd": 0.0, "billing": "invocaciones y GB-segundo"},
        {"service": "AWS Step Functions", "resources": _count(resources, "AWS::StepFunctions::StateMachine"),
         "fixedMonthlyUsd": 0.0, "billing": "transiciones de estado"},
        {"service": "Amazon SQS", "resources": _count(resources, "AWS::SQS::Queue"),
         "fixedMonthlyUsd": 0.0, "billing": "requests"},
    ]

    tagged = {"available": False, "totalUsd": None, "services": [], "estimated": False}
    try:
        tagged = _tagged_cost(clients["ce"], stack_name, start, end)
        if not tagged["available"]:
            warnings.append(
                "Cost Explorer no devolvió cargos para CostStack; la clave puede no estar activa en la cuenta pagadora. "
                "Se usó el cruce manual CloudFormation + servicios de cuenta."
            )
    except Exception as exc:
        warnings.append(f"No se pudo leer el gasto etiquetado de Cost Explorer: {exc}")

    account_service_costs: Optional[Dict[str, Any]] = None
    try:
        account_service_costs = _account_service_costs(clients["ce"], start, end)
    except Exception as exc:
        warnings.append(f"No se pudo leer el gasto por servicio para el fallback manual: {exc}")

    # Cost Explorer no ofrece RESOURCE_ID sin el opt-in del payer. Sólo usamos
    # sus cargos de WAF/KMS cuando el inventario demuestra que todos los
    # recursos facturables activos de esa clase pertenecen a este stack.
    fixed_mtd_actual: Optional[float] = 0.0 if not fixed_items else None
    fixed_mtd_by_service: Dict[str, float] = {}
    actual_scope: List[str] = []
    waf_exclusive = not stack_waf_arns
    kms_exclusive = not stack_key_ids
    try:
        usage_rows, ce_estimated = _ce_usage_rows(clients["ce"], start, end)
        actual = 0.0
        # Un stack que no declara ningún componente de renta fija tiene un
        # fijo real de $0. No depende de CostStack ni debe mostrarse como dato
        # ausente: es una conclusión del inventario CloudFormation.
        has_actual = not fixed_items
        if has_actual:
            actual_scope.append("sin recursos de renta fija en el stack")
        all_wafs = _all_waf_arns(clients["waf"], clients["cloudfront_waf"])
        if stack_waf_arns and all_wafs == stack_waf_arns:
            waf_exclusive = True
            waf_actual = sum(
                row["cost"] for row in usage_rows
                if row["usageType"].endswith("RuleV2") or row["usageType"].endswith("WebACLV2")
            )
            actual += waf_actual
            fixed_mtd_by_service["AWS WAF"] = waf_actual
            has_actual = True
            actual_scope.append("WAF exclusivo de la cuenta")
        elif stack_waf_arns:
            warnings.append("Hay Web ACLs ajenos al stack; el MTD de WAF de la cuenta no se atribuyó.")

        all_customer_keys = _enabled_customer_keys(clients["kms"])
        if stack_key_ids and all_customer_keys == stack_key_ids:
            kms_exclusive = True
            kms_actual = sum(row["cost"] for row in usage_rows if row["usageType"].endswith("KMS-Keys"))
            actual += kms_actual
            fixed_mtd_by_service["AWS Key Management Service"] = kms_actual
            has_actual = True
            actual_scope.append("CMK activa exclusiva de la cuenta/región")
        elif stack_key_ids:
            warnings.append("Hay CMK activas ajenas al stack; el MTD de almacenamiento KMS no se atribuyó.")
        fixed_mtd_actual = round(actual, 12) if has_actual else None
    except Exception as exc:
        ce_estimated = False
        warnings.append(f"No se pudo comprobar el fijo prorrateado en Cost Explorer: {exc}")

    resource_counts = {
        service: sum(_count(resources, resource_type) for resource_type in resource_types)
        for service, resource_types in SERVICE_RESOURCE_TYPES.items()
    }
    manual_services: List[Dict[str, Any]] = []
    if account_service_costs is not None:
        for service, resource_types in SERVICE_RESOURCE_TYPES.items():
            count = resource_counts[service]
            if not count:
                continue
            scoped = fixed_mtd_by_service.get(service)
            manual_services.append({
                "service": service,
                "resources": count,
                "resourceTypes": list(resource_types),
                "accountMtdUsd": account_service_costs["costs"].get(service, 0.0),
                "scopedMtdUsd": scoped,
                "scope": (
                    "atribuido: inventario exclusivo"
                    if scoped is not None
                    else "contexto de cuenta; no sumado sin evidencia por recurso"
                ),
            })
        manual_services.sort(key=lambda row: (-row["accountMtdUsd"], row["service"]))

    fixed_monthly = sum(item["monthlyUsd"] for item in fixed_items)
    try:
        fixed_history = _fixed_cost_history(
            clients["ce"], period_start=period_start, period_end=period_end, today=today,
            fixed_monthly_usd=fixed_monthly,
            has_waf=bool(stack_waf_arns), waf_exclusive=waf_exclusive,
            has_kms=bool(stack_key_ids), kms_exclusive=kms_exclusive,
        )
    except Exception as exc:
        fixed_history = {
            "start": period_start.isoformat(), "end": period_end.isoformat(),
            "available": False, "estimated": False, "actualToDateUsd": None,
            "projectedPeriodUsd": None, "currentRunRateMonthlyUsd": fixed_monthly,
            "rows": [], "scope": f"No se pudo reconstruir el histórico: {exc}",
        }
        warnings.append(f"No se pudo reconstruir el gasto fijo mensual: {exc}")
    if fixed_monthly == 0:
        excluded = []
        if account_service_costs is not None:
            for service in ("AWS WAF", "AWS Key Management Service"):
                value = account_service_costs["costs"].get(service, 0.0)
                if value and resource_counts[service] == 0:
                    excluded.append(f"{service} ${value:.4f} MTD")
        suffix = f" Los cargos de cuenta ({'; '.join(excluded)}) pertenecen a otros recursos/ambientes." if excluded else ""
        environment_explanation = (
            f"{stack_name} ({environment}) no declara Web ACL ni clave KMS propia; por eso su piso fijo es $0."
            f"{suffix}"
        )
    else:
        environment_explanation = (
            f"{stack_name} ({environment}) declara {len(stack_waf_arns)} Web ACL, {waf_rules} reglas WAF y "
            f"{len(stack_key_ids)} clave(s) KMS: piso ${fixed_monthly:.2f}/mes."
        )

    return {
        "stack": stack_name,
        "environment": environment,
        "region": region,
        "capturedAt": captured.isoformat(),
        "monthToDate": {"start": start, "endExclusive": end},
        "pricingAsOf": PRICING_AS_OF,
        "priceSources": PRICE_SOURCES,
        "fixedMonthlyUsd": fixed_monthly,
        "fixedMtdActualUsd": fixed_mtd_actual,
        "fixedMtdEstimated": ce_estimated,
        "fixedActualScope": actual_scope,
        "fixedItems": fixed_items,
        "fixedMonthlyHistory": fixed_history,
        "usageOnlyItems": usage_only,
        "taggedMtd": tagged,
        "manualMtd": {
            "available": account_service_costs is not None,
            "totalScopedUsd": (
                round(sum(row["scopedMtdUsd"] for row in manual_services if row["scopedMtdUsd"] is not None), 12)
                if account_service_costs is not None else None
            ),
            "services": manual_services,
            "estimated": account_service_costs["estimated"] if account_service_costs is not None else False,
            "note": "Los importes de cuenta no se suman al stack salvo que la exclusividad del recurso esté demostrada.",
        },
        "environmentExplanation": environment_explanation,
        "resourceCount": len(resources),
        "warnings": warnings,
    }
