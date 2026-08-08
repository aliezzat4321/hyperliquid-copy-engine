from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from hlcopy.hyperliquid.websocket import HyperliquidMarketCollector
from hlcopy.market.tape import AsyncMarketTapeSink, MarketTapeWriter


async def capture_market(
    *,
    ws_url: str,
    coins: Iterable[str],
    output_dir: Path,
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
        heartbeat_seconds=heartbeat_seconds,
        reconnect_base_seconds=reconnect_base_seconds,
        reconnect_max_seconds=reconnect_max_seconds,
    )
    await collector.run()
