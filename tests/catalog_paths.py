from pathlib import Path


def active_catalog_path(repo_root: Path, type_id: str) -> Path:
    """Return the one non-save catalog file for TYPE_ID."""
    catalogs_dir = repo_root / "catalogs"
    matches = [
        path
        for path in catalogs_dir.rglob(f"{type_id}.json")
        if "save" not in path.relative_to(catalogs_dir).parts
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one active catalog for {type_id}, found {len(matches)}"
        )
    return matches[0]
