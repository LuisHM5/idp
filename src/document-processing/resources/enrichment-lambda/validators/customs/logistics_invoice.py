"""Catalog-driven validator shim — rules live in the catalogs/ schema files."""

from validators.customs import validate_document

__all__ = ["validate_document"]
