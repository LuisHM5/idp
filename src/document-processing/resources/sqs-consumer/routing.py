"""Pure catalog-routing rules for the S3/SQS ingress."""


def route_from_catalog_item(item: dict | None) -> str:
    """Return the safe workflow route represented by one DynamoDB item.

    DynamoDB AttributeValue envelopes are kept here deliberately: this helper
    can be unit-tested without importing boto3 or constructing AWS clients.
    Unknown, disabled and legacy entries always fail closed to the standard
    upstream classification/extraction route.
    """
    item = item or {}
    if item.get("enabled", {}).get("BOOL") is False:
        return "standard"
    if "M" in item.get("fields", {}):
        return "enrichment-only"
    if item.get("pipeline", {}).get("S") == "expediente":
        return "enrichment-only"
    return "standard"
