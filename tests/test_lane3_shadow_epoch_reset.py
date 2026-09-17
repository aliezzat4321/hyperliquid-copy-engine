from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "reset_lane3_shadow_epoch.py"
SPEC = importlib.util.spec_from_file_location("reset_lane3_shadow_epoch", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load Lane 3 reset helper from {SCRIPT_PATH}")
RESET_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESET_MODULE)

DELETE_FILES = RESET_MODULE.DELETE_FILES
SELECTOR_VERSION = RESET_MODULE.SELECTOR_VERSION
assert_shadow_only_env = RESET_MODULE.assert_shadow_only_env
reset_state_root = RESET_MODULE.reset_state_root


def write(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def test_reset_clears_derived_shadow_data_but_preserves_source_evidence(
    tmp_path: Path,
) -> None:
    state = {
        "seen": ["post-1:open", "post-2:close"],
        "managed": {
            "source-a": {
                "sourceBaseId": "source-a",
                "coin": "BTC",
                "side": "long",
            }
        },
        "feedCursors": {
            "following": {
                "postId": "post-2",
                "observedAtMs": 123,
                "source": "following",
            }
        },
        "feedBaselines": {"following": 123, "trending": 456},
    }
    write(tmp_path / "state.json", json.dumps(state))

    preserved = {
        "trader-population.json": '{"traders":{"alice":{}}}\n',
        "invo-leaderboards.json": '{"leaderboardVersion":"raw-source"}\n',
        "invo-leaderboard-snapshots.jsonl": '{"surface":"1D"}\n',
    }
    for name, content in preserved.items():
        write(tmp_path / name, content)

    for name in DELETE_FILES:
        write(tmp_path / name, f"old derived data for {name}\n")
    write(tmp_path / "state.pre-old-epoch-1.json", "old state\n")
    write(tmp_path / "audit.pre-old-epoch-1.jsonl", "old audit\n")
    historical_tracker = tmp_path / "trader-population.pre-old-epoch-1.json"
    write(historical_tracker, "historical discovery evidence\n")

    result = reset_state_root(
        tmp_path,
        epoch="lane3-hybrid-v3-clean-20260916",
        now=dt.datetime(
            2026, 9, 16, 20, 0, tzinfo=dt.timezone.utc  # noqa: UP017
        ),
    )

    after = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert after["seen"] == state["seen"]
    assert after["feedCursors"] == state["feedCursors"]
    assert after["feedBaselines"] == state["feedBaselines"]
    assert after["managed"] == {}
    for name, content in preserved.items():
        assert (tmp_path / name).read_text(encoding="utf-8") == content
    for name in DELETE_FILES:
        assert not (tmp_path / name).exists()
    assert not (tmp_path / "state.pre-old-epoch-1.json").exists()
    assert not (tmp_path / "audit.pre-old-epoch-1.jsonl").exists()
    assert historical_tracker.read_text(encoding="utf-8") == (
        "historical discovery evidence\n"
    )

    assert result["managedPositionsRemoved"] == 1
    assert result["seenKeysPreserved"] == 2
    assert result["feedCursorsPreserved"] == 1
    assert result["feedBaselinesPreserved"] == 2
    assert result["selectorVersion"] == SELECTOR_VERSION
    assert result["realTradingEnabled"] is False
    assert result["polymarketTouched"] is False

    tombstones = (tmp_path / "dataset-resets.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(tombstones) == 1
    assert json.loads(tombstones[0])["epoch"] == "lane3-hybrid-v3-clean-20260916"


def test_invalid_state_fails_before_any_derived_file_is_deleted(
    tmp_path: Path,
) -> None:
    write(tmp_path / "state.json", '{"seen":{},"managed":{},"feedCursors":{}}')
    write(tmp_path / "audit.jsonl", "must remain on failed validation\n")

    with pytest.raises(RuntimeError, match="seen must be an array"):
        reset_state_root(tmp_path, epoch="test-epoch")

    assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8") == (
        "must remain on failed validation\n"
    )
    assert not (tmp_path / "dataset-resets.jsonl").exists()


def test_reset_accepts_legacy_state_without_feed_baselines(tmp_path: Path) -> None:
    legacy = {
        "seen": ["legacy-key"],
        "managed": {},
        "feedCursors": {
            "following": {
                "postId": "legacy-post",
                "observedAtMs": 123,
                "source": "startup_baseline",
            }
        },
    }
    write(tmp_path / "state.json", json.dumps(legacy))

    result = reset_state_root(tmp_path, epoch="legacy-state-reset")

    after = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert after["feedCursors"] == legacy["feedCursors"]
    assert "feedBaselines" not in after
    assert result["feedCursorsPreserved"] == 1
    assert result["feedBaselinesPreserved"] == 0


@pytest.mark.parametrize(
    "invalid", [[], "123", None, {"following": False}, {"following": float("inf")}]
)
def test_invalid_feed_baselines_fail_before_deletion(
    tmp_path: Path, invalid: object
) -> None:
    state = {
        "seen": [],
        "managed": {},
        "feedCursors": {},
        "feedBaselines": invalid,
    }
    write(tmp_path / "state.json", json.dumps(state))
    write(tmp_path / "audit.jsonl", "must remain on failed validation\n")

    with pytest.raises(RuntimeError, match="feedBaselines"):
        reset_state_root(tmp_path, epoch="invalid-baselines")

    assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8") == (
        "must remain on failed validation\n"
    )
    assert not (tmp_path / "dataset-resets.jsonl").exists()


def test_reset_requires_shadow_only_runtime_flags(tmp_path: Path) -> None:
    env_file = tmp_path / "executor.env"
    write(
        env_file,
        "REAL_TRADING_ENABLED=NO\nNOTIFICATION_TRADER_LIVE=false\n",
    )
    assert_shadow_only_env(env_file)

    write(
        env_file,
        "REAL_TRADING_ENABLED=YES\nNOTIFICATION_TRADER_LIVE=false\n",
    )
    with pytest.raises(RuntimeError, match="REAL_TRADING_ENABLED must be NO"):
        assert_shadow_only_env(env_file)

    write(
        env_file,
        "REAL_TRADING_ENABLED=NO\nNOTIFICATION_TRADER_LIVE=true\n",
    )
    with pytest.raises(RuntimeError, match="NOTIFICATION_TRADER_LIVE must be false"):
        assert_shadow_only_env(env_file)
