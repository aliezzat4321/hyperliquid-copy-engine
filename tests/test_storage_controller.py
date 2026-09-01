from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "storage_controller_under_test", ROOT / "scripts/storage_controller.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
POLICY = json.loads((ROOT / "config/storage_governance_v1.json").read_text())
NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def test_policy_governs_every_dataset_and_has_hysteresis() -> None:
    writers = MODULE.validate_policy(POLICY)
    assert len(writers) >= 8
    cfg = POLICY["policy"]
    assert (
        cfg["healthy_used_pct"]
        < cfg["resume_used_pct"]
        < cfg["warn_used_pct"]
        < cfg["stop_used_pct"]
    )
    assert all(row["owner"] and row["retention_class"] for row in POLICY["datasets"])


def test_forecast_pauses_all_writers_before_stop_threshold() -> None:
    previous = {"observed_at": (NOW - timedelta(hours=1)).isoformat(), "used_bytes": 790}
    result = MODULE.evaluate(
        POLICY,
        total_bytes=1000,
        used_bytes=800,
        previous=previous,
        now=NOW,
        paused=False,
    )
    assert result["used_pct"] == 80.0
    assert result["forecast_breach"] is True
    assert result["state"] == "PAUSE"
    assert result["writers_paused"] is True
    assert result["governed_writers"] == MODULE.validate_policy(POLICY)


def test_hysteresis_holds_then_resumes() -> None:
    held = MODULE.evaluate(
        POLICY, total_bytes=1000, used_bytes=790, previous=None, now=NOW, paused=True
    )
    assert held["state"] == "HOLD_PAUSED"
    resumed = MODULE.evaluate(
        POLICY, total_bytes=1000, used_bytes=770, previous=None, now=NOW, paused=True
    )
    assert resumed["state"] == "RESUME"
    assert resumed["writers_paused"] is False


def test_policy_fails_closed_for_ungoverned_dataset() -> None:
    broken = json.loads(json.dumps(POLICY))
    broken["datasets"][0]["writers"] = []
    with pytest.raises(ValueError, match="no governed writer"):
        MODULE.validate_policy(broken)
