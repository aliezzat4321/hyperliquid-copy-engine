from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Iterable
from pathlib import Path
from typing import Any

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


async def _run_collector_with_sigterm(
    collector: Any,
    *,
    signal_loop: Any | None = None,
) -> None:
    """Turn routine SIGTERM into cancellation so collector ``finally`` can flush.

    systemd stops the capture service with SIGTERM under storage pressure. The
    default Python SIGTERM disposition exits immediately, bypassing the collector's
    ``finally: await sink.close()`` path. Installing an event-loop handler keeps
    shutdown cooperative without changing unexpected external task cancellation.
    """
    task = asyncio.create_task(collector.run(), name="hyperliquid-market-collector")
    loop = signal_loop or asyncio.get_running_loop()
    signal_requested = False
    installed = False

    def request_stop() -> None:
        nonlocal signal_requested
        signal_requested = True
        task.cancel()

    try:
        try:
            loop.add_signal_handler(signal.SIGTERM, request_stop)
            installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            # Non-main-thread and non-Unix callers keep their platform default.
            # Production is Linux/main-thread and must install this handler.
            installed = False
        try:
            await task
        except asyncio.CancelledError:
            if not signal_requested:
                raise
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)


async def capture_market(
    *,
    ws_url: str,
    coins: Iterable[str],
    output_dir: Path,
    subscription_types: Iterable[str] | None = None,
    flush_rows: int = 5_000,
    flush_seconds: float = 120.0,
    queue_size: int = 50_000,
    max_buffered_rows: int | None = None,
    heartbeat_seconds: float = 30.0,
    reconnect_base_seconds: float = 1.0,
    reconnect_max_seconds: float = 30.0,
) -> None:
    sink = AsyncMarketTapeSink(
        MarketTapeWriter(output_dir),
        flush_rows=flush_rows,
        flush_seconds=flush_seconds,
        queue_size=queue_size,
        max_buffered_rows=max_buffered_rows,
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
    await _run_collector_with_sigterm(collector)
