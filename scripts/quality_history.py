"""Persistencia y agregación del historial local de calidad.

Los artefactos pueden contener datos aduanales. Por eso viven en una carpeta
ignorada por git y se crean con permisos privados; el dashboard recibe sólo
las métricas agregadas y nombres de checks, no valores extraídos.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DEFAULT_HISTORY = REPO / "resultados-pruebas"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def catalog_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted((REPO / "catalogs").glob("*.json")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def git_context() -> dict[str, Any]:
    def git(*args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=REPO, text=True, capture_output=True)
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"

    status = git("status", "--porcelain")
    return {
        "commit": git("rev-parse", "HEAD"),
        "shortCommit": git("rev-parse", "--short", "HEAD"),
        "branch": git("branch", "--show-current"),
        "dirty": bool(status and status != "unknown"),
    }


def create_run(suite: str, env: str, history_root: Path = DEFAULT_HISTORY) -> tuple[Path, dict[str, Any]]:
    git = git_context()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"{stamp}-{git['shortCommit']}-{suite}"
    run_dir = history_root / env / run_id
    suffix = 1
    while run_dir.exists():
        run_dir = history_root / env / f"{run_id}-{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, mode=0o700)
    for name in ("junit", "coverage", "logs", "cases"):
        (run_dir / name).mkdir(mode=0o700)
    record = {
        "schemaVersion": 1,
        "runId": run_dir.name,
        "suite": suite,
        "environment": env,
        "startedAt": utc_now(),
        "status": "RUNNING",
        "git": git,
        "catalogSha256": catalog_hash(),
        "commands": [],
        "summary": {},
        "cases": [],
    }
    write_record(run_dir, record)
    return run_dir, record


def write_record(run_dir: Path, record: dict[str, Any]) -> None:
    target = run_dir / "run.json"
    temp = run_dir / ".run.json.tmp"
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(target)


def load_runs(env: str, history_root: Path = DEFAULT_HISTORY) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for path in sorted((history_root / env).glob("*/run.json")) if (history_root / env).exists() else []:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            row["_path"] = str(path.parent)
            runs.append(row)
        except (OSError, ValueError, TypeError):
            continue
    return runs


def quality_snapshot(runs: list[dict[str, Any]]) -> dict[str, Any]:
    finished = [r for r in runs if r.get("status") != "RUNNING"]
    latest = finished[-1] if finished else None
    real = [r for r in finished if r.get("suite") == "real-e2e"]
    free = [r for r in finished if r.get("suite") == "quality-free"]
    latest_real = real[-1] if real else None
    latest_free = free[-1] if free else None
    trend = []
    for run in finished:
        s = run.get("summary") or {}
        trend.append({
            "runId": run.get("runId"), "at": run.get("completedAt") or run.get("startedAt"),
            "suite": run.get("suite"), "status": run.get("status"),
            "tests": s.get("tests", 0), "failures": s.get("failures", 0),
            "lineCoverage": s.get("lineCoverage"), "branchCoverage": s.get("branchCoverage"),
            "nodeLineCoverage": s.get("nodeLineCoverage"), "nodeBranchCoverage": s.get("nodeBranchCoverage"),
            "fieldAccuracy": s.get("fieldAccuracy"), "e2ePassRate": s.get("e2ePassRate"),
            "costUsd": s.get("costUsd"), "durationSeconds": s.get("durationSeconds"),
        })
    regressions = []
    if latest_real:
        comparable = [r for r in real[:-1] if r.get("catalogSha256") == latest_real.get("catalogSha256")][-5:]
        if len(comparable) >= 3:
            for field, label in (("costUsd", "costo"), ("durationSeconds", "duración")):
                history = [(r.get("summary") or {}).get(field) for r in comparable]
                history = [float(x) for x in history if x is not None]
                current = (latest_real.get("summary") or {}).get(field)
                if len(history) >= 3 and current is not None:
                    baseline = statistics.median(history)
                    delta = (float(current) / baseline - 1) if baseline else 0
                    if delta > 0.20:
                        regressions.append({"metric": field, "label": label, "current": current,
                                            "baseline": baseline, "delta": delta,
                                            "severity": "critical" if delta > 0.50 else "warning"})
    return {"runs": len(finished), "latest": latest, "latestFree": latest_free,
            "latestReal": latest_real, "trend": trend, "regressions": regressions}
