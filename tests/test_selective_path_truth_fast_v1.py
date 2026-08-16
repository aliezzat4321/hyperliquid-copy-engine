from decimal import Decimal as D

from hlcopy.profitability.continuous_path_v2 import AssetContextMark
from hlcopy.profitability.portfolio_position_copy import FollowerStateEvent
from hlcopy.profitability.selective_path_truth_fast_cli import (
    _active_intervals,
    _deferred_truth_payload,
    _marks_for_active_intervals,
)


def _state(ts: int, qty: str, tid: int) -> FollowerStateEvent:
    return FollowerStateEvent(
        coin="BTC",
        execution_ts_ms=ts // 1_000_000,
        execution_received_at_ns=ts,
        source_tid=tid,
        action="INCREASE" if D(qty) else "CLOSE",
        qty_after=D(qty),
        avg_entry_after=D("100") if D(qty) else None,
        realized_net_pnl_cumulative_usd=D("0"),
        entry_fee_remaining_usd=D("0"),
    )


def _mark(ts: int) -> AssetContextMark:
    return AssetContextMark(
        coin="BTC",
        received_at_ns=ts,
        mark_price=D("100"),
        oracle_price=D("100"),
    )


def test_active_interval_marks_exclude_inactive_tail_but_keep_boundary_context() -> None:
    states = [_state(100, "1", 1), _state(300, "0", 2)]
    intervals = _active_intervals(states, 1_000)
    marks = tuple(_mark(ts) for ts in (50, 100, 150, 250, 350, 900))

    selected = _marks_for_active_intervals(
        marks_by_coin={"BTC": marks},
        intervals=intervals,
    )

    assert intervals == {"BTC": ((100, 300),)}
    assert [row.received_at_ns for row in selected] == [100, 150, 250, 350]
    assert 900 not in [row.received_at_ns for row in selected]


def test_open_position_interval_extends_to_current_end() -> None:
    intervals = _active_intervals([_state(100, "1", 1)], 900)
    assert intervals == {"BTC": ((100, 900),)}


def test_deferred_truth_is_fail_closed() -> None:
    row = _deferred_truth_payload(fee_complete=True)
    assert row["coverage"]["complete"] is False
    assert row["safe_leverage"] is None
    assert "continuous_mtm" in row["validation_blockers"]
    assert "round_trip_fee_accounting" not in row["validation_blockers"]
