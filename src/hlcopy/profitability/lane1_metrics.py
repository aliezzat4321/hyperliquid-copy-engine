from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from hlcopy.profitability.portfolio_position_copy import PortfolioCopySimulation

D = Decimal
ZERO = D("0")
BPS = D("10000")
LANE1_RETURN_BASIS = "COMPLETED_ROUND_TRIP_NET_RETURN_ON_EPISODE_PEAK_GROSS_V1"


@dataclass(frozen=True, slots=True)
class CompletedRoundTripMetrics:
    completed_round_trips: int
    completed_net_pnl_usd: Decimal
    completed_peak_gross_usd: Decimal
    return_bps: Decimal | None


def completed_round_trip_metrics(sim: PortfolioCopySimulation) -> CompletedRoundTripMetrics:
    """Measure only fully completed follower episodes on deployed gross exposure.

    A realized slice is not a round trip: partial reductions may create many slices for
    one position. Lane 1 selection therefore accumulates all realized PnL inside a
    follower coin episode and recognizes it only when that episode returns to flat.
    The denominator is the sum of each completed episode's peak gross exposure, so the
    score cannot rise mechanically just because the observation window contains more
    trades or a trader splits one exit into more reductions.

    Unresolved/open episodes are deliberately excluded from both numerator and
    denominator. Their realized/open economics must be handled by prospective MTM and
    promotion gates rather than leaking partial outcomes into candidate selection.
    """
    realized_by_event: dict[tuple[str, int], Decimal] = defaultdict(lambda: ZERO)
    for realized in sim.realized_slices:
        realized_by_event[(realized.coin, realized.source_tid)] += realized.net_pnl_usd

    episode_pnl: dict[str, Decimal] = defaultdict(lambda: ZERO)
    episode_peak_gross: dict[str, Decimal] = defaultdict(lambda: ZERO)
    completed_pnl = ZERO
    completed_peak_gross = ZERO
    completed = 0

    for state in sim.state_events:
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
                completed_peak_gross += peak
            episode_pnl[state.coin] = ZERO
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
        return_bps=return_bps,
    )
