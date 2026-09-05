from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hlcopy.storage.metrics import DiskUsage

PATH = Path(__file__).parents[1] / "scripts" / "storage_controller.py"
SPEC = importlib.util.spec_from_file_location("storage_controller_test", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def policy(tmp_path: Path) -> dict:
    return {"schema_version": 2, "mounts": [{"name": "data", "path": str(tmp_path),
        "warn_used_pct": 75, "resume_used_pct": 78, "stop_used_pct": 85,
        "target_used_pct": 75, "minimum_forecast_hours": 48, "history_window_hours": 24,
        "unallocated_reserve_bytes": 100, "unaccounted_budget_bytes": 8000}],
        "datasets": [{"name": "tape", "mount": "data", "path": "tape", "owner": "data",
        "writer": "capture", "retention_class": "LIFECYCLE", "byte_budget": 6000,
        "steady_state_bytes": 5000, "growth_budget_bytes_per_hour": 10,
        "pressure_control": "STOP_WRITER", "retention_horizon_hours": 100,
        "reclaim_actuator": "lifecycle"}, {"name": "fills", "mount": "data", "path": "fills",
        "owner": "db", "writer": "fills", "retention_class": "KEEP_DURABLE",
        "byte_budget": 1000, "steady_state_bytes": 500, "growth_budget_bytes_per_hour": 5,
        "pressure_control": "NEVER_STOP", "retention_horizon_hours": 100,
        "reclaim_actuator": "none"}]}


def prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sizes=(100, 100)) -> None:
    (tmp_path / "tape").mkdir(exist_ok=True)
    (tmp_path / "fills").mkdir(exist_ok=True)
    lookup = {"tape": sizes[0], "fills": sizes[1]}
    monkeypatch.setattr(MODULE, "_du", lambda path: lookup[path.name])
    monkeypatch.setattr(MODULE, "disk_usage", lambda _: DiskUsage(10_000, 5000, 5000, 50.0))


def test_smoothed_growth_requires_three_samples_and_never_stop_excluded(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch, (130, 100))
    value = policy(tmp_path)
    value["datasets"][0]["growth_budget_bytes_per_hour"] = 9
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    history = [{"observed_at": (now - timedelta(hours=n)).isoformat(), "pressure_active": False,
                "datasets": [
                    {"name": "tape", "bytes": 100 + (3 - n) * 10},
                    {"name": "fills", "bytes": 100},
                ]}
               for n in (2, 1)]
    result = MODULE.decide(value, history, now=now)
    assert result["pressure_active"]
    assert result["datasets"][0]["bytes_per_hour"] == 10
    assert result["controlled_writers"] == ["capture"]


def test_infeasible_budget_sum_rejected(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch)
    value = policy(tmp_path)
    value["datasets"][0]["byte_budget"] = 7000
    value["datasets"][0]["steady_state_bytes"] = 5000
    with pytest.raises(ValueError, match="exceed target capacity"):
        MODULE.decide(value, [], now=datetime.now(UTC), allow_baseline_without_previous=True)


def test_unaccounted_space_breach(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch)
    value = policy(tmp_path)
    value["mounts"][0]["unaccounted_budget_bytes"] = 100
    result = MODULE.decide(value, [], now=datetime.now(UTC), allow_baseline_without_previous=True)
    assert result["mounts"][0]["unaccounted_budget_breached"]
    assert result["pressure_active"]


def test_zero_measured_growth_is_unbounded_forecast(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch)
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    history = [{"observed_at": (now - timedelta(hours=n)).isoformat(),
                "pressure_active": False,
                "datasets": [{"name": "tape", "bytes": 100},
                             {"name": "fills", "bytes": 100}]}
               for n in (2, 1)]
    result = MODULE.decide(policy(tmp_path), history, now=now)
    assert result["mounts"][0]["hours_to_full"] is None
    assert result["mounts"][0]["forecast_unbounded"] is True


def test_main_writes_fail_closed_decision(tmp_path, monkeypatch):
    output = tmp_path / "decision.json"
    monkeypatch.setattr(
        sys, "argv",
        ["storage_controller", "--policy", str(tmp_path / "missing"),
         "--output", str(output)],
    )
    with pytest.raises(SystemExit) as caught:
        MODULE.main()
    assert caught.value.code == 2
    decision = json.loads(output.read_text())
    assert decision["action"] == "STOP_ALL_MATERIAL_WRITERS"
    assert decision["fail_closed_reason"].startswith("FileNotFoundError")


def test_main_fail_closed_stops_every_policy_writer(tmp_path, monkeypatch):
    output = tmp_path / "decision.json"
    policy_path = tmp_path / "policy.json"
    value = policy(tmp_path)
    policy_path.write_text(json.dumps(value))
    monkeypatch.setattr(
        sys, "argv",
        ["storage_controller", "--policy", str(policy_path), "--output", str(output)],
    )
    with pytest.raises(SystemExit) as caught:
        MODULE.main()
    assert caught.value.code == 2
    decision = json.loads(output.read_text())
    assert decision["controlled_writers"] == ["capture"]
    assert decision["writer_actions"] == [
        {"writer": "capture", "action": "STOP_WRITER"},
    ]
