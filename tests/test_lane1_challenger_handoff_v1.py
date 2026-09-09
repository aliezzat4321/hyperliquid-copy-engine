import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hlcopy.profitability.lane1_handoff import (
    LANE1_SELECTION_CONTRACT_V1,
    build_challenger_queue,
)

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40


def _universe(path: Path, generated_at: datetime, *wallets: str) -> None:
    path.write_text(
        json.dumps({"generated_at": generated_at.isoformat(), "wallets": {w: {} for w in wallets}}),
        encoding="utf-8",
    )


def _robust(wallet: str, coin: str = "BTC") -> dict[str, object]:
    return {
        "wallet_address": wallet,
        "coin": coin,
        "notional_usd": "1000",
        "worst_latency_return_bps": "12",
        "actions_floor": 8,
    }


def _build(robust: list[dict[str, object]], **kwargs: object) -> dict[str, object]:
    return build_challenger_queue(
        robust,
        selection_contract_version=LANE1_SELECTION_CONTRACT_V1,
        **kwargs,  # type: ignore[arg-type]
    )


def test_new_wallet_coin_is_frozen_and_crosses_challenger_handoff(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    universe = tmp_path / "universe.json"
    queue = tmp_path / "challengers.json"
    _universe(universe, now, WALLET_A)

    result = _build(
        [_robust(WALLET_A)],
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 123456,
    )

    assert result["counts"] == {"robust": 1, "challenger": 1, "rejected": 0, "demoted": 0}
    candidate = result["candidates"][0]
    assert candidate["candidate_key"] == f"{LANE1_SELECTION_CONTRACT_V1}|{WALLET_A}|BTC|1000"
    assert candidate["prospective_start_ns"] == 123456

    # A later research run may refresh evidence, but cannot move the frozen cutoff.
    later = _build(
        [_robust(WALLET_A)],
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now + timedelta(hours=1),
        clock_ns=lambda: 999999,
    )
    assert later["candidates"][0]["prospective_start_ns"] == 123456


def test_stale_universe_fails_closed_and_dead_challenger_is_demoted(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    universe = tmp_path / "universe.json"
    queue = tmp_path / "challengers.json"
    _universe(universe, now, WALLET_A)
    _build(
        [_robust(WALLET_A)],
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 1,
    )
    _universe(universe, now - timedelta(hours=7), WALLET_A, WALLET_B)

    result = _build(
        [_robust(WALLET_A), _robust(WALLET_B, "ETH")],
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 2,
    )

    assert result["counts"] == {"robust": 2, "challenger": 0, "rejected": 2, "demoted": 1}
    assert {row["reason"] for row in result["rejections"]} == {"UNIVERSE_STATE_STALE"}
    assert result["demoted"][0]["demotion_reason"] == "NO_LONGER_ROBUST_OR_CURRENT"


def test_future_universe_timestamp_fails_closed(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    universe = tmp_path / "universe.json"
    _universe(universe, now + timedelta(seconds=1), WALLET_A)

    result = _build(
        [_robust(WALLET_A)],
        output_path=tmp_path / "queue.json",
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 3,
    )

    assert result["counts"]["challenger"] == 0
    assert {row["reason"] for row in result["rejections"]} == {"UNIVERSE_TIMESTAMP_FUTURE"}


def test_missing_universe_reason_is_not_masked_as_wallet_absence(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    universe = tmp_path / "missing.json"

    result = _build(
        [_robust(WALLET_A)],
        output_path=tmp_path / "queue.json",
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 4,
    )

    assert result["counts"]["challenger"] == 0
    assert {row["reason"] for row in result["rejections"]} == {"UNIVERSE_STATE_MISSING"}


def test_wallet_coin_selectivity_and_dedupe_are_preserved(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    universe = tmp_path / "universe.json"
    _universe(universe, now, WALLET_A)
    row = _robust(WALLET_A, "SOL")
    result = _build(
        [row, row, _robust(WALLET_B)],
        output_path=tmp_path / "queue.json",
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 5,
    )

    assert [(r["wallet_address"], r["coin"]) for r in result["candidates"]] == [(WALLET_A, "SOL")]
    assert {r["reason"] for r in result["rejections"]} == {
        "DUPLICATE_WALLET_COIN_NOTIONAL",
        "WALLET_NOT_IN_CURRENT_LEADERBOARD",
    }


def test_demotion_persistence_and_reentry_preserve_cutoff_and_history(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    universe = tmp_path / "universe.json"
    queue = tmp_path / "queue.json"
    _universe(universe, now, WALLET_A)

    entered = _build(
        [_robust(WALLET_A)],
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 101,
    )
    entered["candidates"][0]["prospective_outcomes"] = [{"event_count": 7, "approved": False}]
    queue.write_text(json.dumps(entered), encoding="utf-8")

    demoted = _build(
        [],
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now + timedelta(minutes=1),
        clock_ns=lambda: 202,
    )
    persisted = _build(
        [],
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now + timedelta(minutes=2),
        clock_ns=lambda: 303,
    )
    reentered = _build(
        [_robust(WALLET_A)],
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now + timedelta(minutes=3),
        clock_ns=lambda: 404,
    )

    assert demoted["demoted"][0]["prospective_start_ns"] == 101
    assert persisted["demoted"] == demoted["demoted"]
    candidate = reentered["candidates"][0]
    assert candidate["prospective_start_ns"] == 101
    assert candidate["prospective_outcomes"] == [{"event_count": 7, "approved": False}]
    assert [event["status"] for event in candidate["history"]] == [
        "challenger",
        "demoted",
        "challenger",
    ]


def test_repeated_demotion_and_reentry_never_reset_prospective_start(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    universe = tmp_path / "universe.json"
    queue = tmp_path / "queue.json"
    _universe(universe, now, WALLET_A)

    lifecycle = (
        [_robust(WALLET_A)],
        [],
        [_robust(WALLET_A)],
        [],
        [_robust(WALLET_A)],
    )
    for index, robust in enumerate(lifecycle):
        result = _build(
            robust,
            output_path=queue,
            universe_state_path=universe,
            max_universe_age_hours=6,
            now=now + timedelta(minutes=index),
            clock_ns=lambda index=index: 1000 + index,
        )

    candidate = result["candidates"][0]
    assert candidate["prospective_start_ns"] == 1000
    assert [event["status"] for event in candidate["history"]] == [
        "challenger",
        "demoted",
        "challenger",
        "demoted",
        "challenger",
    ]


def test_changed_selection_contract_requires_new_identity(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    universe = tmp_path / "universe.json"
    queue = tmp_path / "queue.json"
    _universe(universe, now, WALLET_A)
    _build(
        [_robust(WALLET_A)],
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 11,
    )

    changed = build_challenger_queue(
        [_robust(WALLET_A)],
        selection_contract_version="lane1-selective-v2",
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now + timedelta(minutes=1),
        clock_ns=lambda: 22,
    )

    assert changed["candidates"][0]["prospective_start_ns"] == 22
    assert changed["demoted"][0]["prospective_start_ns"] == 11
    assert len(changed["candidate_history"]) == 2
    assert {row["selection_contract_version"] for row in changed["candidate_history"]} == {
        LANE1_SELECTION_CONTRACT_V1,
        "lane1-selective-v2",
    }
