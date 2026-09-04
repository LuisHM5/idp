from __future__ import annotations

import json
import sys
from pathlib import Path
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from quality_history import load_runs, quality_snapshot, write_record  # noqa: E402


def test_load_runs_ignores_broken_files_and_sorts(tmp_path: Path) -> None:
    env = tmp_path / "dev"
    (env / "002").mkdir(parents=True)
    (env / "001").mkdir()
    (env / "bad").mkdir()
    (env / "002" / "run.json").write_text(json.dumps({"runId": "002", "startedAt": "2026-02-01", "status": "PASSED"}))
    (env / "001" / "run.json").write_text(json.dumps({"runId": "001", "startedAt": "2026-01-01", "status": "PASSED"}))
    (env / "bad" / "run.json").write_text("{")
    assert [r["runId"] for r in load_runs("dev", tmp_path)] == ["001", "002"]


def test_quality_snapshot_keeps_free_and_real_truth_separate() -> None:
    snapshot = quality_snapshot([
        {"runId": "free", "suite": "quality-free", "status": "PASSED", "summary": {"tests": 12, "lineCoverage": 70}},
        {"runId": "real", "suite": "real-e2e", "status": "FAILED", "summary": {"fieldAccuracy": .8, "e2ePassRate": .5}},
    ])
    assert snapshot["latestFree"]["runId"] == "free"
    assert snapshot["latestReal"]["runId"] == "real"
    assert snapshot["trend"][1]["fieldAccuracy"] == .8


def test_write_record_is_valid_json(tmp_path: Path) -> None:
    write_record(tmp_path, {"status": "PASSED", "summary": {"tests": 1}})
    assert json.loads((tmp_path / "run.json").read_text())["status"] == "PASSED"


def test_regression_requires_three_comparable_real_runs() -> None:
    old = [{"runId": str(i), "suite": "real-e2e", "status": "PASSED", "catalogSha256": "same",
            "summary": {"costUsd": 1.0, "durationSeconds": 10}} for i in range(3)]
    current = {"runId": "now", "suite": "real-e2e", "status": "PASSED", "catalogSha256": "same",
               "summary": {"costUsd": 1.6, "durationSeconds": 11}}
    regressions = quality_snapshot(old + [current])["regressions"]
    assert len(regressions) == 1
    assert regressions[0]["metric"] == "costUsd"
    assert regressions[0]["severity"] == "critical"
    assert regressions[0]["delta"] == pytest.approx(.6)
