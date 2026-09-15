from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl

from hlcopy.market.symbols import wire_coin
from hlcopy.profitability.portfolio_position_copy import PortfolioCopySimulation

D = Decimal
ZERO = D("0")
HOUR_MS = 3_600_000


class FundingEvidenceError(RuntimeError):
    """Raised when a completed follower episode cannot be priced for funding exactly."""


@dataclass(frozen=True, slots=True)
class FundingSettlement:
    coin: str
    time_ms: int
    funding_rate: Decimal
    oracle_px: Decimal
    oracle_received_at_ns: int


@dataclass(frozen=True, slots=True)
class FundingCashflow:
    coin: str
    time_ms: int
    qty: Decimal
    funding_rate: Decimal
    oracle_px: Decimal
    pnl_usd: Decimal


def _utc_dates(start_ms: int, end_ms: int) -> tuple[str, ...]:
    start = datetime.fromtimestamp(start_ms / 1000, tz=UTC).date()
    end = datetime.fromtimestamp(end_ms / 1000, tz=UTC).date()
    dates: list[str] = []
    cursor = start
    while cursor <= end:
        dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return tuple(dates)


def _active_ctx_rows(
    market_dir: Path,
    coin: str,
    *,
    start_ms: int,
    end_ms: int,
) -> list[tuple[int, Decimal]]:
    """Load only the causal oracle observations needed around funding boundaries."""
    wire = wire_coin(coin)
    paths: list[Path] = []
    # Include the previous UTC date because a boundary just after midnight may need
    # the latest observation from immediately before midnight.
    extended_start = max(0, start_ms - 60_000)
    for day in _utc_dates(extended_start, end_ms):
        directory = market_dir / f"date={day}" / f"coin={wire}" / "channel=activeAssetCtx"
        paths.extend(sorted(directory.glob("*.parquet")))
    if not paths:
        return []
    frame = pl.concat(
        [
            pl.read_parquet(path, columns=["received_at_ns", "oracle_px"])
            for path in paths
        ],
        how="vertical_relaxed",
    )
    rows: list[tuple[int, Decimal]] = []
    for row in frame.sort("received_at_ns").iter_rows(named=True):
        received_at_ns = row.get("received_at_ns")
        oracle_px = row.get("oracle_px")
        if received_at_ns is None or oracle_px is None:
            continue
        rows.append((int(received_at_ns), D(str(oracle_px))))
    return rows


def resolve_funding_settlements(
    market_dir: Path,
    coin: str,
    funding_history_rows: Iterable[dict[str, object]],
    *,
    max_oracle_age_ms: int = 10_000,
) -> tuple[FundingSettlement, ...]:
    """Join exact official hourly funding rates to locally captured oracle prices.

    ``fundingHistory`` supplies the finalized hourly rate. Hyperliquid settles using
    spot oracle price, not mark price. We therefore join each finalized rate to the
    latest locally observed ``activeAssetCtx.oracle_px`` at or before that boundary.
    A stale/missing oracle fails closed instead of silently substituting mark/mid/L2.
    """
    parsed: list[tuple[int, Decimal]] = []
    for raw in funding_history_rows:
        if not isinstance(raw, dict):
            continue
        row_coin = str(raw.get("coin", coin))
        if wire_coin(row_coin) != wire_coin(coin):
            continue
        try:
            time_ms = int(raw["time"])
            rate = D(str(raw["fundingRate"]))
        except (KeyError, TypeError, ValueError):
            continue
        parsed.append((time_ms, rate))
    if not parsed:
        return ()
    parsed.sort(key=lambda item: item[0])
    oracle_rows = _active_ctx_rows(
        market_dir,
        coin,
        start_ms=parsed[0][0],
        end_ms=parsed[-1][0],
    )
    settlements: list[FundingSettlement] = []
    oracle_index = 0
    latest: tuple[int, Decimal] | None = None
    for time_ms, rate in parsed:
        boundary_ns = time_ms * 1_000_000
        while oracle_index < len(oracle_rows) and oracle_rows[oracle_index][0] <= boundary_ns:
            latest = oracle_rows[oracle_index]
            oracle_index += 1
        if rate == ZERO:
            # Zero funding creates no cashflow. Preserve a deterministic placeholder
            # oracle only when one is available; otherwise no price is economically needed.
            if latest is None:
                settlements.append(
                    FundingSettlement(coin, time_ms, rate, ZERO, boundary_ns)
                )
                continue
        if latest is None:
            raise FundingEvidenceError(
                f"missing causal oracle for {coin} funding boundary {time_ms}"
            )
        oracle_received_at_ns, oracle_px = latest
        age_ms = time_ms - oracle_received_at_ns / 1_000_000
        if age_ms < 0 or age_ms > max(0, max_oracle_age_ms):
            raise FundingEvidenceError(
                f"stale causal oracle for {coin} funding boundary {time_ms}: age_ms={age_ms:.3f}"
            )
        if oracle_px <= ZERO:
            raise FundingEvidenceError(f"invalid oracle for {coin} funding boundary {time_ms}")
        settlements.append(
            FundingSettlement(
                coin=coin,
                time_ms=time_ms,
                funding_rate=rate,
                oracle_px=oracle_px,
                oracle_received_at_ns=oracle_received_at_ns,
            )
        )
    return tuple(settlements)


def _completed_episode_boundaries(sim: PortfolioCopySimulation) -> tuple[tuple[str, int, int], ...]:
    opened_at: dict[str, int] = {}
    completed: list[tuple[str, int, int]] = []
    for state in sorted(
        sim.state_events,
        key=lambda row: (row.execution_ts_ms, row.execution_received_at_ns, row.source_tid),
    ):
        coin = state.coin
        if state.qty_after != ZERO and coin not in opened_at:
            opened_at[coin] = state.execution_ts_ms
        if state.qty_after == ZERO and state.action in {"CLOSE", "FLIP_CLOSE"}:
            start_ms = opened_at.pop(coin, None)
            if start_ms is not None:
                completed.append((coin, start_ms, state.execution_ts_ms))
        if state.action == "FLIP_OPEN" and state.qty_after != ZERO:
            opened_at[coin] = state.execution_ts_ms
    return tuple(completed)


def _hour_boundaries_after(start_ms: int, end_ms: int) -> tuple[int, ...]:
    first = ((start_ms // HOUR_MS) + 1) * HOUR_MS
    if first > end_ms:
        return ()
    return tuple(range(first, end_ms + 1, HOUR_MS))


def validate_completed_episode_funding_coverage(
    sim: PortfolioCopySimulation,
    settlements: Iterable[FundingSettlement],
) -> None:
    available = {(row.coin, row.time_ms) for row in settlements}
    missing: list[str] = []
    for coin, start_ms, end_ms in _completed_episode_boundaries(sim):
        for boundary in _hour_boundaries_after(start_ms, end_ms):
            if (coin, boundary) not in available:
                missing.append(f"{coin}@{boundary}")
    if missing:
        raise FundingEvidenceError(
            "missing finalized funding evidence for completed follower episode(s): "
            + ",".join(missing[:20])
        )


def funding_cashflows(
    sim: PortfolioCopySimulation,
    settlements: Iterable[FundingSettlement],
) -> tuple[FundingCashflow, ...]:
    """Reconstruct follower funding cashflows at hourly settlement boundaries.

    Settlement is applied before an execution sharing the exact same millisecond. This
    is conservative for closes at the boundary and prevents an opening at that same
    boundary from receiving/paying funding before it exists on exchange.
    """
    ordered_states = sorted(
        sim.state_events,
        key=lambda row: (row.execution_ts_ms, row.execution_received_at_ns, row.source_tid),
    )
    ordered_settlements = sorted(settlements, key=lambda row: (row.time_ms, row.coin))
    qty_by_coin: dict[str, Decimal] = {}
    state_index = 0
    cashflows: list[FundingCashflow] = []
    for settlement in ordered_settlements:
        while (
            state_index < len(ordered_states)
            and ordered_states[state_index].execution_ts_ms < settlement.time_ms
        ):
            state = ordered_states[state_index]
            qty_by_coin[state.coin] = state.qty_after
            state_index += 1
        qty = qty_by_coin.get(settlement.coin, ZERO)
        if qty == ZERO or settlement.funding_rate == ZERO:
            continue
        # Positive rate: longs pay shorts. Negative rate: shorts pay longs.
        pnl = -qty * settlement.oracle_px * settlement.funding_rate
        cashflows.append(
            FundingCashflow(
                coin=settlement.coin,
                time_ms=settlement.time_ms,
                qty=qty,
                funding_rate=settlement.funding_rate,
                oracle_px=settlement.oracle_px,
                pnl_usd=pnl,
            )
        )
    return tuple(cashflows)
