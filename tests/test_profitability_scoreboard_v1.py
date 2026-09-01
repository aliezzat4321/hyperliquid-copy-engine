from __future__ import annotations

import copy
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/ai-team/profitability_scoreboard.json"
OUTPUT = ROOT / "docs/ai-team/PROFITABILITY_SCOREBOARD.md"
SCRIPT = ROOT / "scripts/render_profitability_scoreboard.py"

spec = importlib.util.spec_from_file_location("render_profitability_scoreboard", SCRIPT)
assert spec and spec.loader
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def _data() -> dict:
    return json.loads(SOURCE.read_text(encoding="utf-8"))


def test_scoreboard_is_complete_and_rendered_without_drift() -> None:
    data = _data()
    renderer.validate(data)
    assert renderer.render(data) == OUTPUT.read_text(encoding="utf-8")


def test_scoreboard_covers_every_lane_once() -> None:
    data = _data()
    assert [row["lane"] for row in sorted(data["lanes"], key=lambda row: row["rank"])] == [
        "lane_3", "lane_1", "lane_2"
    ]


def test_live_trading_cannot_be_enabled() -> None:
    data = _data()
    data["live_trading_enabled"] = True
    with pytest.raises(ValueError, match="must not enable"):
        renderer.validate(data)


def test_missing_required_economics_fails_validation() -> None:
    data = copy.deepcopy(_data())
    del data["lanes"][0]["outcomes"]
    with pytest.raises(ValueError, match="missing fields"):
        renderer.validate(data)


@pytest.mark.parametrize(
    "field",
    [
        "as_of", "ranking_basis", "priority_candidates", "promotion_demotion",
        "decision",
    ],
)
def test_missing_rendered_top_level_field_fails_validation(field: str) -> None:
    data = copy.deepcopy(_data())
    del data[field]
    with pytest.raises(ValueError, match="missing fields"):
        renderer.validate(data)


@pytest.mark.parametrize("field", ["name", "evidence_level"])
def test_missing_rendered_lane_field_fails_validation(field: str) -> None:
    data = copy.deepcopy(_data())
    del data["lanes"][0][field]
    with pytest.raises(ValueError, match="missing fields"):
        renderer.validate(data)


def test_malformed_lane_fails_with_value_error() -> None:
    data = copy.deepcopy(_data())
    data["lanes"][0] = None
    with pytest.raises(ValueError, match="must be an object"):
        renderer.validate(data)


def test_malformed_as_of_fails_validation() -> None:
    data = copy.deepcopy(_data())
    data["as_of"] = "not-a-timestamp"
    with pytest.raises(ValueError, match="RFC3339"):
        renderer.validate(data)


def test_stale_as_of_fails_validation() -> None:
    data = copy.deepcopy(_data())
    as_of = datetime.fromisoformat(data["as_of"].replace("Z", "+00:00"))
    now = as_of + timedelta(hours=renderer.MAX_SNAPSHOT_AGE_HOURS + 1)
    with pytest.raises(ValueError, match="stale"):
        renderer.validate(data, now=now)


def test_current_as_of_passes_validation() -> None:
    data = _data()
    as_of = datetime.fromisoformat(data["as_of"].replace("Z", "+00:00"))
    renderer.validate(data, now=as_of.astimezone(UTC) + timedelta(hours=1))


def test_unknown_evidence_level_fails_validation() -> None:
    data = copy.deepcopy(_data())
    data["lanes"][0]["evidence_level"] = "EXPLORATORY_SHADOW"
    with pytest.raises(ValueError, match="evidence_level"):
        renderer.validate(data)
