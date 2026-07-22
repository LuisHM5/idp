"""Catalog-driven validator shim — rules live in customs_catalog.json."""

from validators.customs import validate_document

__all__ = ["validate_document"]
