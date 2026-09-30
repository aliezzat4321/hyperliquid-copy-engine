import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hlcopy.profitability.incremental_funnel_cli import _selection_return_bps, _split_oos
from hlcopy.profitability.lane1_handoff import (
    LANE1_SELECTION_CONTRACT_V1,
    build_challenger_queue,
    record_prospective_outcomes,
)
from hlcopy.profitability.position_copy import CopyFillEvent
from hlcopy.profitability.position_live_cli import NOTIONALS

WALLET = "0x" + "a" * 40
D = Decimal


def _universe(path: Path, now: datetime) -> None:
    path.write_text(
        json.dumps({"generated_at": now.isoformat(), "wallets": {WALLET: {}}}),
        encoding="utf-8",
    )


def _robust(notional: str = "25000") -> dict[str, object]:
    return {
        "wallet_address": WALLET,
        "coin": "HYPE",
        "notional_usd": notional,
        "worst_latency_return_bps": "4.2",
        "actions_floor": 25,
    }


def _event(received_at_ns: int, tid: int) -> CopyFillEvent:
    return CopyFillEvent(
        lane="WIDE",
        wallet_id="wide",
        wallet_address=WALLET,
        coin="HYPE",
        exchange_ts_ms=received_at_ns // 1_000_000,
        received_at_ns=received_at_ns,
        tid=tid,
        leader_start=D("0"),
        leader_after=D("1"),
        leader_delta=D("1"),
        source_price=D("1"),
    )


def test_canonical_notional_grid_contains_high_primary_notionals() -> None:
    assert tuple(str(value) for value in NOTIONALS) == (
        "1000",
        "5000",
        "10000",
        "25000",
        "50000",
    )


def test_selection_return_is_normalized_by_action_count() -> None:
    summary = {"realized_actions": 5, "closed_net_pnl_usd": "50"}
    assert _selection_return_bps(summary, D("1000")) == D("100")
    summary["realized_actions"] = 10
    assert _selection_return_bps(summary, D("1000")) == D("50")


def test_screen_and_confirmation_windows_are_strictly_disjoint() -> None:
    events = tuple(_event(index * 1_000_000_000, index) for index in range(1, 11))
    split = _split_oos(events, min_screen_events=4, min_confirm_events=3)
    assert split is not None
    screen, confirm = split
    assert len(screen) >= 4
    assert len(confirm) >= 3
    assert {row.tid for row in screen}.isdisjoint({row.tid for row in confirm})
    assert screen[-1].received_at_ns < confirm[0].received_at_ns


def test_prospective_outcome_is_written_to_identity_ledger(tmp_path: Path) -> None:
    now = datetime(2026, 9, 14, 18, tzinfo=UTC)
    universe = tmp_path / "universe.json"
    queue = tmp_path / "queue.json"
    _universe(universe, now)
    created = build_challenger_queue(
        [_robust()],
        selection_contract_version=LANE1_SELECTION_CONTRACT_V1,
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 100,
    )
    key = created["candidates"][0]["candidate_key"]
    outcome = {
        "candidate_key": key,
        "observed_at": now.isoformat(),
        "event_count": 31,
        "evaluation_state": "EVALUATED",
        "actions_floor": 25,
        "worst_primary_return_bps": "-1.2",
        "approved": False,
        "evidence_fingerprint": "same-evidence",
    }
    record_prospective_outcomes(queue, [outcome])
    record_prospective_outcomes(queue, [outcome])

    persisted = json.loads(queue.read_text(encoding="utf-8"))
    assert persisted["candidates"][0]["prospective_outcomes"] == [outcome]
    assert persisted["candidate_history"][0]["prospective_outcomes"] == [outcome]
    assert persisted["candidates"][0]["prospective_start_ns"] == 100


def test_evaluated_underperformer_is_demoted_without_resetting_cutoff(tmp_path: Path) -> None:
    now = datetime(2026, 9, 14, 18, tzinfo=UTC)
    universe = tmp_path / "universe.json"
    queue = tmp_path / "queue.json"
    _universe(universe, now)
    created = build_challenger_queue(
        [_robust()],
        selection_contract_version=LANE1_SELECTION_CONTRACT_V1,
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 101,
    )
    key = created["candidates"][0]["candidate_key"]
    record_prospective_outcomes(
        queue,
        [
            {
                "candidate_key": key,
                "observed_at": now.isoformat(),
                "event_count": 40,
                "evaluation_state": "EVALUATED",
                "actions_floor": 20,
                "worst_primary_return_bps": "-0.1",
                "approved": False,
                "evidence_fingerprint": "loser",
            }
        ],
    )

    refreshed = build_challenger_queue(
        [_robust()],
        selection_contract_version=LANE1_SELECTION_CONTRACT_V1,
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 999,
    )
    assert refreshed["candidates"] == []
    assert refreshed["rejections"][0]["reason"] == "PROSPECTIVE_UNDERPERFORM"
    assert refreshed["demoted"][0]["demotion_reason"] == "PROSPECTIVE_UNDERPERFORM"
    assert refreshed["demoted"][0]["prospective_start_ns"] == 101


def test_insufficient_actions_do_not_become_performance_failure(tmp_path: Path) -> None:
    now = datetime(2026, 9, 14, 18, tzinfo=UTC)
    universe = tmp_path / "universe.json"
    queue = tmp_path / "queue.json"
    _universe(universe, now)
    created = build_challenger_queue(
        [_robust()],
        selection_contract_version=LANE1_SELECTION_CONTRACT_V1,
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 102,
    )
    key = created["candidates"][0]["candidate_key"]
    record_prospective_outcomes(
        queue,
        [
            {
                "candidate_key": key,
                "observed_at": now.isoformat(),
                "event_count": 4,
                "evaluation_state": "INSUFFICIENT_ACTIONS",
                "actions_floor": 0,
                "worst_primary_return_bps": None,
                "approved": None,
                "evidence_fingerprint": "insufficient",
            }
        ],
    )

    refreshed = build_challenger_queue(
        [_robust()],
        selection_contract_version=LANE1_SELECTION_CONTRACT_V1,
        output_path=queue,
        universe_state_path=universe,
        max_universe_age_hours=6,
        now=now,
        clock_ns=lambda: 999,
    )
    assert len(refreshed["candidates"]) == 1
    assert refreshed["candidates"][0]["prospective_start_ns"] == 102
