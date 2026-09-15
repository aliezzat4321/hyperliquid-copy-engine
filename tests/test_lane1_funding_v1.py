from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from hlcopy.profitability.lane1_funding import (
    FundingEvidenceError,
    FundingSettlement,
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


def _slice(*, ts: int, pnl: str) -> RealizedSlice:
    return RealizedSlice(
        lane="WIDE",
        wallet_id="wide",
        wallet_address=WALLET,
        coin="HYPE",
        direction="LONG",
        exchange_ts_ms=ts,
        source_tid=ts,
        feed_ms=10.0,
        action="CLOSE",
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
