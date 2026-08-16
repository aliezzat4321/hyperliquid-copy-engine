from decimal import Decimal as D

from hlcopy.profitability.continuous_path_v2 import AssetContextMark
from hlcopy.profitability.margin_tables import (
    CoinMarginTable,
    MarginMetadataSnapshot,
    MarginTier,
)
from hlcopy.profitability.path_truth import (
    evaluate_candidate_path_truth,
    evaluate_candidate_path_truth_exact,
)
from hlcopy.profitability.portfolio_position_copy import FollowerStateEvent


def _state(
    ts_ns: int,
    *,
    qty: str,
    entry: str | None,
    realized: str,
    fee: str,
    tid: int,
    action: str,
) -> FollowerStateEvent:
    return FollowerStateEvent(
        coin="BTC",
        execution_ts_ms=ts_ns // 1_000_000,
        execution_received_at_ns=ts_ns,
        source_tid=tid,
        action=action,
        qty_after=D(qty),
        avg_entry_after=D(entry) if entry is not None else None,
        realized_net_pnl_cumulative_usd=D(realized),
        entry_fee_remaining_usd=D(fee),
    )


def _mark(ts_ns: int, price: str) -> AssetContextMark:
    return AssetContextMark(
        coin="BTC",
        received_at_ns=ts_ns,
        mark_price=D(price),
        oracle_price=D(price),
    )


def _margin_snapshot() -> MarginMetadataSnapshot:
    tier = MarginTier(
        lower_bound_usd=D("0"),
        max_leverage=D("10"),
        maintenance_margin_rate=D("0.05"),
        maintenance_deduction_usd=D("0"),
    )
    return MarginMetadataSnapshot(
        fetched_at_ns=500_000_000,
        tables=(CoinMarginTable("BTC", 52, (tier,)),),
    )


def test_streaming_path_truth_matches_materialized_reference() -> None:
    states = (
        _state(
            1_000_000_000,
            qty="2",
            entry="100",
            realized="0",
            fee="1",
            tid=1,
            action="INCREASE",
        ),
        _state(
            3_000_000_000,
            qty="1",
            entry="100",
            realized="5",
            fee="0.5",
            tid=2,
            action="REDUCE",
        ),
        _state(
            5_000_000_000,
            qty="0",
            entry=None,
            realized="12",
            fee="0",
            tid=3,
            action="CLOSE",
        ),
    )
    marks = (
        _mark(900_000_000, "99"),
        _mark(1_000_000_000, "100"),
        _mark(2_000_000_000, "103"),
        _mark(3_000_000_000, "101"),
        _mark(4_000_000_000, "106"),
        _mark(5_000_000_000, "107"),
    )
    kwargs = dict(
        state_events=states,
        asset_contexts=marks,
        funding_rates=(),
        margin_snapshots=(_margin_snapshot(),),
        leverages=(D("1"), D("2"), D("5"), D("10"), D("20")),
        round_trip_fee_accounting=True,
    )

    exact = evaluate_candidate_path_truth_exact(**kwargs).to_dict()
    streaming = evaluate_candidate_path_truth(**kwargs).to_dict()

    assert streaming == exact
    assert streaming["coverage"]["checkpoint_count"] == 4
    assert streaming["safe_leverage"]["max_safe_leverage"] == "10"


def test_streaming_path_truth_preserves_fail_closed_missing_margin() -> None:
    states = (
        _state(
            1_000_000_000,
            qty="1",
            entry="100",
            realized="0",
            fee="0",
            tid=1,
            action="INCREASE",
        ),
    )
    marks = (
        _mark(1_000_000_000, "100"),
        _mark(2_000_000_000, "101"),
    )
    kwargs = dict(
        state_events=states,
        asset_contexts=marks,
        funding_rates=(),
        margin_snapshots=(),
        leverages=(D("1"), D("2")),
        round_trip_fee_accounting=True,
    )

    exact = evaluate_candidate_path_truth_exact(**kwargs).to_dict()
    streaming = evaluate_candidate_path_truth(**kwargs).to_dict()

    assert streaming == exact
    assert streaming["coverage"]["complete"] is False
