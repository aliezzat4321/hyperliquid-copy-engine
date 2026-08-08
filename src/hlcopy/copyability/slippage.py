from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

D = Decimal
ZERO = D("0")
BPS = D("10000")


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class ExecutionEstimate:
    side: str
    requested_size: Decimal
    filled_size: Decimal
    vwap: Decimal | None
    worst_price: Decimal | None
    slippage_bps: Decimal | None
    complete: bool


def estimate_marketable_fill(
    *,
    side: str,
    quantity: Decimal,
    levels: list[BookLevel],
    reference_mid: Decimal,
    max_slippage_bps: Decimal | None = None,
) -> ExecutionEstimate:
    """Walk executable book levels for a follower marketable order.

    `levels` must be asks ascending for BUY and bids descending for SELL. If a slippage
    cap is supplied, liquidity beyond that price is considered unavailable rather than
    pretending the follower receives a fill.
    """
    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if quantity <= ZERO:
        raise ValueError("quantity must be positive")
    if reference_mid <= ZERO:
        raise ValueError("reference_mid must be positive")

    remaining = quantity
    notional = ZERO
    filled = ZERO
    worst: Decimal | None = None

    for level in levels:
        if level.price <= ZERO or level.size <= ZERO:
            continue
        raw_slippage = (
            (level.price - reference_mid) / reference_mid
            if side == "BUY"
            else (reference_mid - level.price) / reference_mid
        )
        level_slippage_bps = raw_slippage * BPS
        if max_slippage_bps is not None and level_slippage_bps > max_slippage_bps:
            break
        take = min(remaining, level.size)
        notional += take * level.price
        filled += take
        remaining -= take
        worst = level.price
        if remaining == ZERO:
            break

    vwap = notional / filled if filled else None
    if vwap is None:
        slippage = None
    elif side == "BUY":
        slippage = (vwap - reference_mid) / reference_mid * BPS
    else:
        slippage = (reference_mid - vwap) / reference_mid * BPS
    return ExecutionEstimate(
        side=side,
        requested_size=quantity,
        filled_size=filled,
        vwap=vwap,
        worst_price=worst,
        slippage_bps=slippage,
        complete=filled == quantity,
    )
