import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hlcopy.profitability.lane1_handoff import build_challenger_queue

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


def test_new_wallet_coin_is_frozen_and_crosses_challenger_handoff(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    universe = tmp_path / "universe.json"
    queue = tmp_path / "challengers.json"
    _universe(universe, now, WALLET_A)

    result = build_challenger_queue(
        [_robust(WALLET_A)],
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 123456,
    )

    assert result["counts"] == {"robust": 1, "challenger": 1, "rejected": 0, "demoted": 0}
    candidate = result["candidates"][0]
    assert candidate["candidate_key"] == f"{WALLET_A}|BTC|1000"
    assert candidate["prospective_start_ns"] == 123456

    # A later research run may refresh evidence, but cannot move the frozen cutoff.
    later = build_challenger_queue(
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
    build_challenger_queue(
        [_robust(WALLET_A)],
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 1,
    )
    _universe(universe, now - timedelta(hours=7), WALLET_A, WALLET_B)

    result = build_challenger_queue(
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


def test_wallet_coin_selectivity_and_dedupe_are_preserved(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    universe = tmp_path / "universe.json"
    _universe(universe, now, WALLET_A)
    row = _robust(WALLET_A, "SOL")
    result = build_challenger_queue(
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
