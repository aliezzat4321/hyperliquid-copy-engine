from __future__ import annotations

import time
from typing import Any

from hlcopy.shadow.wide_watch import HyperliquidWideTradeCollector


class ProspectiveWideTradeCollector(HyperliquidWideTradeCollector):
    """Live-only public trade collector.

    Hyperliquid can replay recent market data when subscriptions start or reconnect.
    Those rows are useful for gap recovery, but they are not prospective copy signals.
    This wrapper fails closed: anything that predates this process or arrives too late
    is ignored by the low-latency research lane.
    """

    def __init__(self, *args: Any, max_live_lag_ms: float = 2_000.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.started_ms = int(time.time() * 1000)
        self.max_live_lag_ms = max(0.0, float(max_live_lag_ms))
        self.ignored_prestart = 0
        self.ignored_stale = 0

    async def _record_trade(
        self,
        *,
        trade: dict[str, Any],
        tracked: dict[str, Any],
        received_at_ns: int,
        received_monotonic_ns: int,
    ) -> None:
        try:
            exchange_ts_ms = int(trade["time"])
        except (KeyError, TypeError, ValueError):
            return

        if exchange_ts_ms < self.started_ms:
            self.ignored_prestart += 1
            return

        observed_lag_ms = received_at_ns / 1_000_000 - exchange_ts_ms
        if observed_lag_ms < 0 or observed_lag_ms > self.max_live_lag_ms:
            self.ignored_stale += 1
            return

        await super()._record_trade(
            trade=trade,
            tracked=tracked,
            received_at_ns=received_at_ns,
            received_monotonic_ns=received_monotonic_ns,
        )
