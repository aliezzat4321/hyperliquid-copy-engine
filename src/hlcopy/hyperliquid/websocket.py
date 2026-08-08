from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Iterable
from typing import Any, Protocol

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from hlcopy.market.normalize import (
    TradeDeduper,
    build_subscriptions,
    normalize_market_message,
    system_record,
)

logger = logging.getLogger(__name__)


class MarketSink(Protocol):
    async def start(self) -> None: ...

    async def put(self, row: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...


class HyperliquidMarketCollector:
    """Reconnect-safe public market-data collector with explicit gap markers."""

    def __init__(
        self,
        ws_url: str,
        coins: Iterable[str],
        sink: MarketSink,
        *,
        heartbeat_seconds: float = 30.0,
        reconnect_base_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
    ) -> None:
        self.ws_url = ws_url
        self.subscriptions = build_subscriptions(coins)
        if not self.subscriptions:
            raise ValueError("at least one market coin is required")
        self.sink = sink
        self.heartbeat_seconds = max(5.0, heartbeat_seconds)
        self.reconnect_base_seconds = max(0.1, reconnect_base_seconds)
        self.reconnect_max_seconds = max(self.reconnect_base_seconds, reconnect_max_seconds)
        self._deduper = TradeDeduper()

    async def run(self) -> None:
        await self.sink.start()
        attempt = 0
        try:
            while True:
                try:
                    await self._run_connection()
                    await self.sink.put(system_record("connection_lost", "stream ended"))
                    logger.warning("Hyperliquid WebSocket stream ended; reconnecting")
                    attempt = 0
                except asyncio.CancelledError:
                    raise
                except (WebSocketException, OSError, TimeoutError) as exc:
                    await self.sink.put(system_record("connection_lost", type(exc).__name__))
                    logger.warning("Hyperliquid WebSocket lost: %s", type(exc).__name__)
                except Exception as exc:
                    await self.sink.put(system_record("fatal_collector_error", repr(exc)))
                    raise
                attempt += 1
                delay = min(
                    self.reconnect_max_seconds,
                    self.reconnect_base_seconds * (2 ** min(attempt - 1, 6)),
                )
                delay += random.uniform(0, delay * 0.25)
                await self.sink.put(system_record("reconnect_wait", f"{delay:.3f}s"))
                logger.info("reconnecting to Hyperliquid in %.3fs", delay)
                await asyncio.sleep(delay)
        finally:
            await self.sink.close()

    async def _run_connection(self) -> None:
        async with connect(
            self.ws_url,
            ping_interval=None,
            open_timeout=10,
            close_timeout=5,
            max_queue=4096,
        ) as websocket:
            await self.sink.put(system_record("connection_open", self.ws_url))
            logger.info("connected to Hyperliquid WebSocket: %s", self.ws_url)
            for subscription in self.subscriptions:
                payload = {"method": "subscribe", "subscription": subscription}
                await websocket.send(json.dumps(payload, separators=(",", ":")))
            heartbeat = asyncio.create_task(self._heartbeat(websocket), name="hl-heartbeat")
            try:
                async for raw_message in websocket:
                    received_at_ns = time.time_ns()
                    received_monotonic_ns = time.monotonic_ns()
                    try:
                        message = json.loads(raw_message)
                    except (json.JSONDecodeError, TypeError):
                        await self.sink.put(system_record("invalid_json", str(raw_message)[:500]))
                        continue
                    if not isinstance(message, dict):
                        continue
                    channel = message.get("channel")
                    if channel == "subscriptionResponse":
                        detail = json.dumps(message.get("data"), separators=(",", ":"))
                        await self.sink.put(system_record("subscription_ack", detail))
                        continue
                    if channel == "pong":
                        continue
                    rows = normalize_market_message(
                        message,
                        received_at_ns=received_at_ns,
                        received_monotonic_ns=received_monotonic_ns,
                    )
                    for row in rows:
                        if not self._deduper.seen(row):
                            await self.sink.put(row)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, websocket: Any) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            await websocket.send('{"method":"ping"}')
