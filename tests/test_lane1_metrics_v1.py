from decimal import Decimal
from types import SimpleNamespace

from hlcopy.profitability.lane1_metrics import completed_round_trip_metrics
from hlcopy.profitability.portfolio_position_copy import FollowerStateEvent
from hlcopy.profitability.position_copy import RealizedSlice

D = Decimal
WALLET = "0x" + "a" * 40


def _slice(*, tid: int, action: str, pnl: str) -> RealizedSlice:
    return RealizedSlice(
        lane="WIDE",
        wallet_id="wide",
        wallet_address=WALLET,
        coin="HYPE",
        direction="LONG",
        exchange_ts_ms=tid,
        source_tid=tid,
        feed_ms=10.0,
        action=action,
        qty=D("1"),
        execution_price=D("100"),
        gross_pnl_usd=D(pnl),
        fee_usd=D("0"),
        net_pnl_usd=D(pnl),
        entry_fee_usd_allocated=D("0"),
    )


def _state(*, tid: int, action: str, qty: str, avg: str | None) -> FollowerStateEvent:
    return FollowerStateEvent(
        coin="HYPE",
        execution_ts_ms=tid,
        execution_received_at_ns=tid * 1_000_000,
        source_tid=tid,
        action=action,
        qty_after=D(qty),
        avg_entry_after=D(avg) if avg is not None else None,
        realized_net_pnl_cumulative_usd=D("0"),
        entry_fee_remaining_usd=D("0"),
    )


def test_partial_reductions_form_one_completed_round_trip() -> None:
    sim = SimpleNamespace(
        realized_slices=(
            _slice(tid=2, action="REDUCE", pnl="30"),
            _slice(tid=3, action="CLOSE", pnl="20"),
        ),
        state_events=(
            _state(tid=1, action="INCREASE", qty="10", avg="100"),
            _state(tid=2, action="REDUCE", qty="5", avg="100"),
            _state(tid=3, action="CLOSE", qty="0", avg=None),
        ),
    )

    metrics = completed_round_trip_metrics(sim)

    assert metrics.completed_round_trips == 1
    assert metrics.completed_net_pnl_usd == D("50")
    assert metrics.completed_peak_gross_usd == D("1000")
    assert metrics.return_bps == D("500")


def test_open_episode_is_excluded_from_selection_return() -> None:
    sim = SimpleNamespace(
        realized_slices=(_slice(tid=2, action="REDUCE", pnl="30"),),
        state_events=(
            _state(tid=1, action="INCREASE", qty="10", avg="100"),
            _state(tid=2, action="REDUCE", qty="5", avg="100"),
        ),
    )

    metrics = completed_round_trip_metrics(sim)

    assert metrics.completed_round_trips == 0
    assert metrics.completed_net_pnl_usd == D("0")
    assert metrics.completed_peak_gross_usd == D("0")
    assert metrics.return_bps is None


def test_more_identical_round_trips_do_not_inflate_return_bps() -> None:
    one = SimpleNamespace(
        realized_slices=(_slice(tid=2, action="CLOSE", pnl="50"),),
        state_events=(
            _state(tid=1, action="INCREASE", qty="10", avg="100"),
            _state(tid=2, action="CLOSE", qty="0", avg=None),
        ),
    )
    two = SimpleNamespace(
        realized_slices=(
            _slice(tid=2, action="CLOSE", pnl="50"),
            _slice(tid=4, action="CLOSE", pnl="50"),
        ),
        state_events=(
            _state(tid=1, action="INCREASE", qty="10", avg="100"),
            _state(tid=2, action="CLOSE", qty="0", avg=None),
            _state(tid=3, action="INCREASE", qty="10", avg="100"),
            _state(tid=4, action="CLOSE", qty="0", avg=None),
        ),
    )

    assert completed_round_trip_metrics(one).return_bps == D("500")
    assert completed_round_trip_metrics(two).return_bps == D("500")
