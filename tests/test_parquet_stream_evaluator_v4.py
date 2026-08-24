from decimal import Decimal as D

from hlcopy.profitability.continuous_path_v2 import AssetContextMark
from hlcopy.profitability.margin_tables import (
    CoinMarginTable,
    MarginMetadataSnapshot,
    MarginTier,
)
from hlcopy.profitability.parquet_stream_evaluator import (
    evaluate_candidate_path_truth_from_factory,
)
from hlcopy.profitability.path_truth import evaluate_candidate_path_truth_exact
from hlcopy.profitability.portfolio_position_copy import FollowerStateEvent


def _state(ts_ns: int, *, qty: str, entry: str | None, action: str) -> FollowerStateEvent:
    return FollowerStateEvent(
        coin="BTC",
        execution_ts_ms=ts_ns // 1_000_000,
        execution_received_at_ns=ts_ns,
        source_tid=ts_ns,
        action=action,
        qty_after=D(qty),
        avg_entry_after=D(entry) if entry is not None else None,
        realized_net_pnl_cumulative_usd=D("0"),
        entry_fee_remaining_usd=D("0"),
    )


def _mark(ts_ns: int, price: str, *, coin: str = "BTC") -> AssetContextMark:
    return AssetContextMark(
        coin=coin,
        received_at_ns=ts_ns,
        mark_price=D(price),
        oracle_price=D(price),
    )


def _margin() -> MarginMetadataSnapshot:
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


def test_reopenable_stream_matches_exact_reference() -> None:
    states = (
        _state(1_000_000_000, qty="2", entry="100", action="INCREASE"),
        _state(5_000_000_000, qty="0", entry=None, action="CLOSE"),
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
        funding_rates=(),
        margin_snapshots=(_margin(),),
        leverages=(D("1"), D("2"), D("5"), D("10")),
        round_trip_fee_accounting=True,
    )
    exact = evaluate_candidate_path_truth_exact(
        asset_contexts=marks,
        **kwargs,
    ).to_dict()
    streamed, gaps = evaluate_candidate_path_truth_from_factory(
        mark_factory=lambda: iter(marks),
        **kwargs,
    )
    assert streamed.to_dict() == exact
    assert gaps == {}


def test_reopenable_stream_reports_gap_diagnostics_fail_closed() -> None:
    states = (
        _state(1_000_000_000, qty="1", entry="100", action="INCREASE"),
    )
    marks = (
        _mark(1_000_000_000, "100"),
        _mark(10_000_000_000, "200", coin="ETH"),
        _mark(20_000_000_000, "101"),
    )
    truth, gaps = evaluate_candidate_path_truth_from_factory(
        state_events=states,
        mark_factory=lambda: iter(marks),
        funding_rates=(),
        margin_snapshots=(_margin(),),
        leverages=(D("1"), D("2")),
        round_trip_fee_accounting=True,
        max_mark_age_ns=5_000_000_000,
    )
    payload = truth.to_dict()
    assert payload["coverage"]["complete"] is False
    assert "MARK_GAP:BTC" in payload["coverage"]["blockers"]
    assert gaps["MARK_GAP:BTC"]["max_gap_seconds"] == 9.0
