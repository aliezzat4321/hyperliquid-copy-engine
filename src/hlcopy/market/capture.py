from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from hlcopy.hyperliquid.websocket import (
    DEFAULT_MARKET_SUBSCRIPTION_TYPES,
    HyperliquidMarketCollector,
)
from hlcopy.market.tape import AsyncMarketTapeSink, MarketTapeWriter


def _subscription_types(explicit: Iterable[str] | None) -> tuple[str, ...]:
    if explicit is not None:
        return tuple(explicit)
    configured = os.getenv("HLCOPY_MARKET_SUBSCRIPTION_TYPES", "").strip()
    if configured:
        return tuple(value.strip() for value in configured.split(",") if value.strip())
    return DEFAULT_MARKET_SUBSCRIPTION_TYPES


async def capture_market(
    *,
    ws_url: str,
    coins: Iterable[str],
    output_dir: Path,
    subscription_types: Iterable[str] | None = None,
    flush_rows: int = 5_000,
    flush_seconds: float = 5.0,
    queue_size: int = 50_000,
    heartbeat_seconds: float = 30.0,
    reconnect_base_seconds: float = 1.0,
    reconnect_max_seconds: float = 30.0,
) -> None:
    sink = AsyncMarketTapeSink(
        MarketTapeWriter(output_dir),
        flush_rows=flush_rows,
        flush_seconds=flush_seconds,
        queue_size=queue_size,
    )
    collector = HyperliquidMarketCollector(
        ws_url,
        coins,
        sink,
        subscription_types=_subscription_types(subscription_types),
        heartbeat_seconds=heartbeat_seconds,
        reconnect_base_seconds=reconnect_base_seconds,
        reconnect_max_seconds=reconnect_max_seconds,
    )
    await collector.run()
