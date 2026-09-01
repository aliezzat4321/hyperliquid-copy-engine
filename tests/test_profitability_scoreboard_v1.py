from __future__ import annotations

import copy
import importlib.util
import json
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
