from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from hlcopy.profitability.lane1_funding import (
    FundingEvidenceError,
    FundingSettlement,
    completed_episode_funding_boundaries,
    funding_cashflows,
    resolve_funding_settlements,
    validate_completed_episode_funding_coverage,
)
from hlcopy.profitability.lane1_metrics import completed_round_trip_metrics
from hlcopy.profitability.portfolio_position_copy import FollowerStateEvent
from hlcopy.profitability.position_copy import RealizedSlice

D = Decimal
WALLET = "0x" + "a" * 40
HOUR = 3_600_000


def _state(*, ts: int, action: str, qty: str, avg: str | None) -> FollowerStateEvent:
    return FollowerStateEvent(
        coin="HYPE",
        execution_ts_ms=ts,
        execution_received_at_ns=ts * 1_000_000,
        source_tid=ts,
        action=action,
        qty_after=D(qty),
        avg_entry_after=D(avg) if avg is not None else None,
        realized_net_pnl_cumulative_usd=D("0"),
        entry_fee_remaining_usd=D("0"),
    )


def _slice(*, ts: int, pnl: str, action: str = "CLOSE") -> RealizedSlice:
    return RealizedSlice(
        lane="WIDE",
        wallet_id="wide",
        wallet_address=WALLET,
        coin="HYPE",
        direction="LONG",
        exchange_ts_ms=ts,
        source_tid=ts,
        feed_ms=10.0,
        action=action,
        qty=D("1"),
        execution_price=D("100"),
        gross_pnl_usd=D(pnl),
        fee_usd=D("0"),
        net_pnl_usd=D(pnl),
        entry_fee_usd_allocated=D("0"),
    )


def _sim(*states: FollowerStateEvent, slices: tuple[RealizedSlice, ...] = ()):
    return SimpleNamespace(state_events=states, realized_slices=slices)


def test_positive_funding_rate_long_pays_and_short_receives() -> None:
    settlement = FundingSettlement("HYPE", HOUR, D("0.001"), D("100"), HOUR * 1_000_000)
    long_sim = _sim(
        _state(ts=1, action="INCREASE", qty="2", avg="100"),
        _state(ts=HOUR + 1, action="CLOSE", qty="0", avg=None),
    )
    short_sim = _sim(
        _state(ts=1, action="INCREASE", qty="-2", avg="100"),
        _state(ts=HOUR + 1, action="CLOSE", qty="0", avg=None),
    )

    assert funding_cashflows(long_sim, (settlement,))[0].pnl_usd == D("-0.2")
    assert funding_cashflows(short_sim, (settlement,))[0].pnl_usd == D("0.2")


def test_negative_funding_rate_reverses_long_short_economics() -> None:
    settlement = FundingSettlement("HYPE", HOUR, D("-0.001"), D("100"), HOUR * 1_000_000)
    long_sim = _sim(
        _state(ts=1, action="INCREASE", qty="2", avg="100"),
        _state(ts=HOUR + 1, action="CLOSE", qty="0", avg=None),
    )
    short_sim = _sim(
        _state(ts=1, action="INCREASE", qty="-2", avg="100"),
        _state(ts=HOUR + 1, action="CLOSE", qty="0", avg=None),
    )

    assert funding_cashflows(long_sim, (settlement,))[0].pnl_usd == D("0.2")
    assert funding_cashflows(short_sim, (settlement,))[0].pnl_usd == D("-0.2")


def test_flat_before_funding_boundary_has_no_cashflow() -> None:
    settlement = FundingSettlement("HYPE", HOUR, D("0.001"), D("100"), HOUR * 1_000_000)
    sim = _sim(
        _state(ts=1, action="INCREASE", qty="2", avg="100"),
        _state(ts=HOUR - 1, action="CLOSE", qty="0", avg=None),
    )

    assert funding_cashflows(sim, (settlement,)) == ()


def test_partial_reduction_before_boundary_uses_remaining_exposure() -> None:
    settlement = FundingSettlement("HYPE", HOUR, D("0.001"), D("100"), HOUR * 1_000_000)
    sim = _sim(
        _state(ts=1, action="INCREASE", qty="10", avg="100"),
        _state(ts=HOUR - 1, action="REDUCE", qty="4", avg="100"),
        _state(ts=HOUR + 1, action="CLOSE", qty="0", avg=None),
    )

    flows = funding_cashflows(sim, (settlement,))

    assert len(flows) == 1
    assert flows[0].qty == D("4")
    assert flows[0].pnl_usd == D("-0.4")


def test_multiple_hourly_funding_events_apply_exactly_once_each() -> None:
    settlements = (
        FundingSettlement("HYPE", HOUR, D("0.001"), D("100"), HOUR * 1_000_000),
        FundingSettlement("HYPE", 2 * HOUR, D("0.002"), D("100"), 2 * HOUR * 1_000_000),
    )
    sim = _sim(
        _state(ts=1, action="INCREASE", qty="2", avg="100"),
        _state(ts=2 * HOUR + 1, action="CLOSE", qty="0", avg=None),
    )

    flows = funding_cashflows(sim, settlements)

    assert [row.time_ms for row in flows] == [HOUR, 2 * HOUR]
    assert [row.pnl_usd for row in flows] == [D("-0.2"), D("-0.4")]
    assert sum((row.pnl_usd for row in flows), D("0")) == D("-0.6")


def test_boundary_semantics_charge_existing_position_not_same_ms_open() -> None:
    settlement = FundingSettlement("HYPE", HOUR, D("0.001"), D("100"), HOUR * 1_000_000)
    closes_at_boundary = _sim(
        _state(ts=1, action="INCREASE", qty="2", avg="100"),
        _state(ts=HOUR, action="CLOSE", qty="0", avg=None),
    )
    opens_at_boundary = _sim(
        _state(ts=HOUR, action="INCREASE", qty="2", avg="100"),
        _state(ts=HOUR + 1, action="CLOSE", qty="0", avg=None),
    )

    assert funding_cashflows(closes_at_boundary, (settlement,))[0].pnl_usd == D("-0.2")
    assert funding_cashflows(opens_at_boundary, (settlement,)) == ()


def test_completed_round_trip_return_includes_funding() -> None:
    sim = _sim(
        _state(ts=1, action="INCREASE", qty="10", avg="100"),
        _state(ts=HOUR + 1, action="CLOSE", qty="0", avg=None),
        slices=(_slice(ts=HOUR + 1, pnl="50"),),
    )
    settlement = FundingSettlement("HYPE", HOUR, D("0.001"), D("100"), HOUR * 1_000_000)
    flows = funding_cashflows(sim, (settlement,))
    metrics = completed_round_trip_metrics(sim, funding_cashflows=flows)

    assert metrics.funding_net_pnl_usd == D("-1")
    assert metrics.completed_net_pnl_usd == D("49")
    assert metrics.completed_peak_gross_usd == D("1000")
    assert metrics.return_bps == D("490")


def test_splitting_realized_slice_does_not_change_funding_adjusted_return() -> None:
    close_ts = HOUR + 1
    states = (
        _state(ts=1, action="INCREASE", qty="10", avg="100"),
        _state(ts=close_ts, action="CLOSE", qty="0", avg=None),
    )
    one_slice = _sim(*states, slices=(_slice(ts=close_ts, pnl="50"),))
    split_slices = _sim(
        *states,
        slices=(
            _slice(ts=close_ts, pnl="20", action="REDUCE"),
            _slice(ts=close_ts, pnl="30", action="CLOSE"),
        ),
    )
    settlement = FundingSettlement("HYPE", HOUR, D("0.001"), D("100"), HOUR * 1_000_000)

    one_flows = funding_cashflows(one_slice, (settlement,))
    split_flows = funding_cashflows(split_slices, (settlement,))
    one_metrics = completed_round_trip_metrics(one_slice, funding_cashflows=one_flows)
    split_metrics = completed_round_trip_metrics(split_slices, funding_cashflows=split_flows)

    assert one_flows == split_flows
    assert one_metrics.completed_net_pnl_usd == split_metrics.completed_net_pnl_usd == D("49")
    assert one_metrics.return_bps == split_metrics.return_bps == D("490")


def test_completed_episode_reports_only_exact_crossed_hourly_boundaries() -> None:
    sim = _sim(
        _state(ts=HOUR // 2, action="INCREASE", qty="1", avg="100"),
        _state(ts=2 * HOUR + 1, action="CLOSE", qty="0", avg=None),
    )

    assert completed_episode_funding_boundaries(sim) == (
        ("HYPE", HOUR),
        ("HYPE", 2 * HOUR),
    )


def test_completed_episode_missing_hourly_funding_fails_closed() -> None:
    sim = _sim(
        _state(ts=1, action="INCREASE", qty="1", avg="100"),
        _state(ts=2 * HOUR + 1, action="CLOSE", qty="0", avg=None),
    )
    with pytest.raises(FundingEvidenceError, match="missing finalized funding evidence"):
        validate_completed_episode_funding_coverage(
            sim,
            (FundingSettlement("HYPE", HOUR, D("0"), D("0"), HOUR * 1_000_000),),
        )


def test_finalized_rate_joins_to_latest_causal_oracle(tmp_path: Path) -> None:
    boundary = 1_800_000_000_000
    day = "2027-01-15"
    directory = tmp_path / f"date={day}" / "coin=HYPE" / "channel=activeAssetCtx"
    directory.mkdir(parents=True)
    pl.DataFrame(
        {
            "received_at_ns": [
                (boundary - 5_000) * 1_000_000,
                (boundary + 1_000) * 1_000_000,
            ],
            "oracle_px": [100.0, 999.0],
        }
    ).write_parquet(directory / "part.parquet")

    rows = [{"coin": "HYPE", "time": boundary, "fundingRate": "0.001"}]
    settlements = resolve_funding_settlements(tmp_path, "HYPE", rows, max_oracle_age_ms=10_000)

    assert settlements[0].oracle_px == D("100.0")
    assert settlements[0].funding_rate == D("0.001")


def test_stale_oracle_fails_closed(tmp_path: Path) -> None:
    boundary = 1_800_000_000_000
    day = "2027-01-15"
    directory = tmp_path / f"date={day}" / "coin=HYPE" / "channel=activeAssetCtx"
    directory.mkdir(parents=True)
    pl.DataFrame(
        {
            "received_at_ns": [(boundary - 20_000) * 1_000_000],
            "oracle_px": [100.0],
        }
    ).write_parquet(directory / "part.parquet")

    rows = [{"coin": "HYPE", "time": boundary, "fundingRate": "0.001"}]
    with pytest.raises(FundingEvidenceError, match="stale causal oracle"):
        resolve_funding_settlements(tmp_path, "HYPE", rows, max_oracle_age_ms=10_000)
