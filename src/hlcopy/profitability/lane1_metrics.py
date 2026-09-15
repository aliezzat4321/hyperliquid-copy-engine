from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from hlcopy.profitability.lane1_funding import FundingCashflow
from hlcopy.profitability.portfolio_position_copy import PortfolioCopySimulation

D = Decimal
ZERO = D("0")
BPS = D("10000")
LANE1_RETURN_BASIS = "COMPLETED_ROUND_TRIP_NET_RETURN_ON_EPISODE_PEAK_GROSS_AFTER_FUNDING_V2"


@dataclass(frozen=True, slots=True)
class CompletedRoundTripMetrics:
    completed_round_trips: int
    completed_net_pnl_usd: Decimal
    completed_peak_gross_usd: Decimal
    funding_net_pnl_usd: Decimal
    return_bps: Decimal | None


def completed_round_trip_metrics(
    sim: PortfolioCopySimulation,
    *,
    funding_cashflows: Iterable[FundingCashflow] = (),
) -> CompletedRoundTripMetrics:
    """Measure completed follower episodes after fees and exact funding cashflows.

    A realized slice is not a round trip: partial reductions may create many slices for
    one position. Lane 1 accumulates all realized trade PnL and funding cashflows inside
    a follower coin episode and recognizes them only when that episode returns to flat.
    The denominator is the sum of each completed episode's peak gross exposure, so the
    score cannot rise mechanically merely because the window contains more trades or a
    trader splits one exit into multiple reductions.

    Open/unresolved episodes remain excluded from selection. Funding on those positions
    is not allowed to leak into historical candidate selection before the episode closes.
    """
    realized_by_event: dict[tuple[str, int], Decimal] = defaultdict(lambda: ZERO)
    for realized in sim.realized_slices:
        realized_by_event[(realized.coin, realized.source_tid)] += realized.net_pnl_usd

    ordered_funding = sorted(funding_cashflows, key=lambda row: (row.time_ms, row.coin))
    funding_index = 0
    episode_pnl: dict[str, Decimal] = defaultdict(lambda: ZERO)
    episode_funding: dict[str, Decimal] = defaultdict(lambda: ZERO)
    episode_peak_gross: dict[str, Decimal] = defaultdict(lambda: ZERO)
    completed_pnl = ZERO
    completed_funding = ZERO
    completed_peak_gross = ZERO
    completed = 0

    states = sorted(
        sim.state_events,
        key=lambda row: (row.execution_ts_ms, row.execution_received_at_ns, row.source_tid),
    )
    for state in states:
        while (
            funding_index < len(ordered_funding)
            and ordered_funding[funding_index].time_ms <= state.execution_ts_ms
        ):
            cashflow = ordered_funding[funding_index]
            episode_pnl[cashflow.coin] += cashflow.pnl_usd
            episode_funding[cashflow.coin] += cashflow.pnl_usd
            funding_index += 1

        event_key = (state.coin, state.source_tid)
        event_pnl = realized_by_event.pop(event_key, ZERO)
        if event_pnl != ZERO:
            episode_pnl[state.coin] += event_pnl

        if state.qty_after != ZERO and state.avg_entry_after is not None:
            gross = abs(state.qty_after) * state.avg_entry_after
            if gross > episode_peak_gross[state.coin]:
                episode_peak_gross[state.coin] = gross

        if state.qty_after == ZERO and state.action in {"CLOSE", "FLIP_CLOSE"}:
            peak = episode_peak_gross[state.coin]
            if peak > ZERO:
                completed += 1
                completed_pnl += episode_pnl[state.coin]
                completed_funding += episode_funding[state.coin]
                completed_peak_gross += peak
            episode_pnl[state.coin] = ZERO
            episode_funding[state.coin] = ZERO
            episode_peak_gross[state.coin] = ZERO

    return_bps = (
        completed_pnl / completed_peak_gross * BPS
        if completed > 0 and completed_peak_gross > ZERO
        else None
    )
    return CompletedRoundTripMetrics(
        completed_round_trips=completed,
        completed_net_pnl_usd=completed_pnl,
        completed_peak_gross_usd=completed_peak_gross,
        funding_net_pnl_usd=completed_funding,
        return_bps=return_bps,
    )
