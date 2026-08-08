from __future__ import annotations

import math

from hlcopy.analytics.performance import WalletMetrics


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def copyability_proxy(metrics: WalletMetrics) -> float:
    """Behavior-only proxy used before historical order-book replay exists.

    This score intentionally does *not* claim to model executable follower PnL. It only
    prioritizes wallets worth spending expensive market-data/backtest resources on.
    """
    hold = metrics.median_hold_seconds
    hold_score = 100.0 * (1.0 - math.exp(-hold / 900.0)) if hold > 0 else 0.0
    maker_penalty = 70.0 * max(0.0, (metrics.maker_share - 0.5) / 0.5)
    fast_penalty = 60.0 * metrics.fast_trade_fraction
    frequency_penalty = 30.0 * min(1.0, max(0.0, (metrics.trades_per_day - 50.0) / 150.0))
    fills_penalty = 20.0 * min(1.0, max(0.0, (metrics.fills_per_trade - 15.0) / 35.0))
    return round(_clamp(hold_score - maker_penalty - fast_penalty - frequency_penalty - fills_penalty), 2)
