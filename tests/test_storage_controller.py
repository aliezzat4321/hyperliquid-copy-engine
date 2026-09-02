from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
import sys

import pytest

PATH = Path(__file__).parents[1] / "scripts" / "storage_controller.py"
SPEC = importlib.util.spec_from_file_location("storage_controller_test", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def policy() -> dict:
    return {"schema_version": 1, "warn_used_pct": 75, "resume_used_pct": 78, "stop_used_pct": 85, "minimum_forecast_hours": 48, "history_window_hours": 24, "datasets": [{"name": "tape", "path": "tape", "owner": "data", "writer": "capture", "retention_class": "LIFECYCLE", "byte_budget": 1000, "growth_budget_bytes_per_hour": 100, "pressure_control": "STOP_WRITER"}]}


def test_growth_budget_stops_all_material_writers(tmp_path, monkeypatch):
    (tmp_path / "tape").mkdir()
    monkeypatch.setattr(MODULE, "_du", lambda _: 1200)
    monkeypatch.setattr(MODULE.shutil, "disk_usage", lambda _: MODULE.shutil._ntuple_diskusage(10_000, 5000, 5000))
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    previous = {"observed_at": (now - timedelta(hours=1)).isoformat(), "pressure_active": False, "datasets": [{"name": "tape", "bytes": 900}]}
    result = MODULE.decide(policy(), previous, mount=tmp_path, now=now)
    assert result["action"] == "STOP_ALL_MATERIAL_WRITERS"
    assert result["controlled_writers"] == ["capture"]
    assert result["datasets"][0]["growth_budget_breached"] is True


def test_hysteresis_holds_pressure_until_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "_du", lambda _: 10)
    monkeypatch.setattr(MODULE.shutil, "disk_usage", lambda _: MODULE.shutil._ntuple_diskusage(10_000, 7900, 2100))
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    previous = {"observed_at": (now - timedelta(hours=1)).isoformat(), "pressure_active": True, "datasets": [{"name": "tape", "bytes": 10}]}
    assert MODULE.decide(policy(), previous, mount=tmp_path, now=now)["pressure_active"] is True


def test_policy_fails_closed_for_ungoverned_fields(tmp_path):
    broken = policy()
    del broken["datasets"][0]["owner"]
    with pytest.raises(ValueError, match="missing owner"):
        MODULE.decide(broken, None, mount=tmp_path, now=datetime.now(UTC))


def test_stale_previous_observation_fails_closed(tmp_path, monkeypatch):
    (tmp_path / "tape").mkdir()
    monkeypatch.setattr(MODULE, "_du", lambda _: 100)
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    previous = {
        "observed_at": (now - timedelta(days=30)).isoformat(),
        "pressure_active": False,
        "datasets": [{"name": "tape", "bytes": 0}],
    }
    with pytest.raises(ValueError, match="previous observation is stale"):
        MODULE.decide(policy(), previous, mount=tmp_path, now=now)


def test_missing_governed_dataset_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        MODULE.shutil,
        "disk_usage",
        lambda _: MODULE.shutil._ntuple_diskusage(10_000, 5000, 5000),
    )
    with pytest.raises(FileNotFoundError, match="governed dataset path does not exist"):
        MODULE.decide(policy(), None, mount=tmp_path, now=datetime.now(UTC))
