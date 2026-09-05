from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from hlcopy.copyability.slippage import estimate_marketable_fill

from .reconstruction import ExecutionLeg, ReconstructedPosition

D = Decimal
BPS = D("10000")


class CostCompleteness(StrEnum):
    MEASURED = "MEASURED"
    UNMEASURED_NO_BOOK = "UNMEASURED_NO_BOOK"
    CAPACITY_INFEASIBLE = "CAPACITY_INFEASIBLE"
    UNMEASURED_FUNDING = "UNMEASURED_FUNDING"


class BookProvider(Protocol):
    def at_or_before(self, coin: str, arrival_ms: float): ...


@dataclass(frozen=True, slots=True)
class LegCost:
    leg: ExecutionLeg
    fee_usd: D
    crossing_usd: D | None
    half_spread_usd: D | None
    impact_usd: D | None
    crossing_bps: D | None
    completeness: CostCompleteness
    arrival_timestamp_ms: float = 0.0
    book_received_at_ns: int | None = None
    evidence_basis: str = "UNMEASURED"


def measure_leg(
    position: ReconstructedPosition,
    leg: ExecutionLeg,
    provider: BookProvider,
    *,
    taker_rate: D,
    max_slippage_bps: D,
    follower_submit_latency_ms: float,
    transport_latency_ms: float,
) -> LegCost:
    if follower_submit_latency_ms < 0 or transport_latency_ms < 0:
        raise ValueError("execution latency inputs cannot be negative")
    arrival_ms = leg.timestamp_ms + follower_submit_latency_ms + transport_latency_ms
    book = provider.at_or_before(position.coin, arrival_ms)
    fee = leg.notional * taker_rate
    # Never mislabel a future quote as causal arrival-time execution evidence.
    if book is None or book.received_at_ns > arrival_ms * 1_000_000:
        return LegCost(
            leg, fee, None, None, None, None, CostCompleteness.UNMEASURED_NO_BOOK,
            arrival_ms, None, "UNMEASURED_NO_CAUSAL_ARRIVAL_BOOK",
        )
    entering = leg.kind == "ENTRY"
    buy = (position.side.lower() == "long") == entering
    levels = list(book.asks if buy else book.bids)
    fill = estimate_marketable_fill(
        side="BUY" if buy else "SELL", quantity=leg.size, levels=levels,
        reference_mid=leg.mid, max_slippage_bps=max_slippage_bps,
    )
    if not fill.complete or fill.slippage_bps is None or not levels:
        return LegCost(
            leg, fee, None, None, None, fill.slippage_bps,
            CostCompleteness.CAPACITY_INFEASIBLE, arrival_ms, book.received_at_ns,
            "CAUSAL_SIMULATED_ORDER_ARRIVAL",
        )
    best = levels[0].price
    half_bps = ((best - leg.mid) if buy else (leg.mid - best)) / leg.mid * BPS
    impact_bps = fill.slippage_bps - half_bps
    return LegCost(
        leg, fee, leg.notional * fill.slippage_bps / BPS,
        leg.notional * half_bps / BPS, leg.notional * impact_bps / BPS,
        fill.slippage_bps, CostCompleteness.MEASURED, arrival_ms,
        book.received_at_ns, "CAUSAL_SIMULATED_ORDER_ARRIVAL",
    )


@dataclass(frozen=True, slots=True)
class PositionEconomics:
    gross_mid_to_mid_pnl_usd: D
    entry_fees_usd: D
    exit_fee_usd: D
    entry_crossing_usd: D | None
    exit_crossing_usd: D | None
    funding_usd: D | None
    net_pnl_usd: D | None
    net_return_bps: D | None
    cost_completeness: CostCompleteness


def position_economics(
    position: ReconstructedPosition,
    costs: list[LegCost],
    *,
    funding_usd: D | None,
    funding_measured: bool,
) -> PositionEconomics:
    if position.exit_leg is None:
        raise ValueError("closed position required")
    gross = sum(
        (position.exit_leg.mid - leg.mid) * leg.size * position.direction
        for leg in position.entry_legs
    )
    entries = costs[:-1]
    exit_cost = costs[-1]
    entry_fees = sum((cost.fee_usd for cost in entries), D("0"))
    completeness = CostCompleteness.MEASURED
    for cost in costs:
        if cost.completeness != CostCompleteness.MEASURED:
            completeness = cost.completeness
            break
    if not funding_measured:
        completeness = CostCompleteness.UNMEASURED_FUNDING
    measured = completeness == CostCompleteness.MEASURED
    entry_cross = (
        sum((cost.crossing_usd or D("0") for cost in entries), D("0"))
        if measured
        else None
    )
    exit_cross = exit_cost.crossing_usd if measured else None
    net = (
        gross - entry_fees - exit_cost.fee_usd - entry_cross - exit_cross + funding_usd
        if measured
        and funding_usd is not None
        and entry_cross is not None
        and exit_cross is not None
        else None
    )
    return PositionEconomics(gross, entry_fees, exit_cost.fee_usd, entry_cross, exit_cross,
                             funding_usd if funding_measured else None, net,
                             net / position.entry_notional * BPS if net is not None else None,
                             completeness)


def scenario_net(position: ReconstructedPosition, fee_usd: D, funding_usd: D | None,
                 round_trip_bps: D) -> D | None:
    """Explicit execution-cost scenario; this value must never populate net_pnl_usd."""
    if position.exit_leg is None or funding_usd is None:
        return None
    gross = sum((position.exit_leg.mid - leg.mid) * leg.size * position.direction
                for leg in position.entry_legs)
    # Solve over actual executed notionals, split uniformly only for the scenario label.
    executed = position.entry_notional + position.exit_leg.notional
    return gross - fee_usd - executed * round_trip_bps / (D("2") * BPS) + funding_usd
