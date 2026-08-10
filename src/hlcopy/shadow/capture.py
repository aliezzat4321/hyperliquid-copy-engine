from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from hlcopy.market.capture import capture_market
from hlcopy.shadow.registry import WalletRegistry, WalletSpec

logger = logging.getLogger(__name__)


class JsonlShadowSink:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = asyncio.Lock()

    async def put(self, row: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._append, row)

    def _append(self, row: dict[str, Any]) -> None:
        timestamp_ns = int(row.get("received_at_ns") or time.time_ns())
        day = time.strftime("%Y-%m-%d", time.gmtime(timestamp_ns / 1_000_000_000))
        path = self.root / "fills" / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
            handle.flush()


class _FillDeduper:
    def __init__(self, max_keys: int = 200_000) -> None:
        self.max_keys = max_keys
        self.keys: set[tuple[str, int, int]] = set()
        self.order: deque[tuple[str, int, int]] = deque()

    def seen(self, user: str, fill: dict[str, Any]) -> bool:
        try:
            key = (user.lower(), int(fill["time"]), int(fill["tid"]))
        except (KeyError, TypeError, ValueError):
            return False
        if key in self.keys:
            return True
        self.keys.add(key)
        self.order.append(key)
        while len(self.order) > self.max_keys:
            self.keys.discard(self.order.popleft())
        return False


class HyperliquidWalletFillCollector:
    """Prospective, reconnect-safe public userFills collector for a wallet registry."""

    def __init__(
        self,
        *,
        ws_url: str,
        registry: WalletRegistry,
        sink: JsonlShadowSink,
        reload_seconds: float = 5.0,
        heartbeat_seconds: float = 30.0,
        reconnect_base_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
    ) -> None:
        self.ws_url = ws_url
        self.registry = registry
        self.sink = sink
        self.reload_seconds = max(1.0, reload_seconds)
        self.heartbeat_seconds = max(5.0, heartbeat_seconds)
        self.reconnect_base_seconds = max(0.1, reconnect_base_seconds)
        self.reconnect_max_seconds = max(self.reconnect_base_seconds, reconnect_max_seconds)
        self.started_ms = int(time.time() * 1000)
        self.deduper = _FillDeduper()

    async def run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._run_connection()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except (WebSocketException, OSError, TimeoutError) as exc:
                await self._system("connection_lost", type(exc).__name__)
                logger.warning("wallet fill websocket lost: %s", type(exc).__name__)
            attempt += 1
            delay = min(
                self.reconnect_max_seconds,
                self.reconnect_base_seconds * (2 ** min(attempt - 1, 6)),
            )
            delay += random.uniform(0, delay * 0.25)
            await self._system("reconnect_wait", f"{delay:.3f}s")
            await asyncio.sleep(delay)

    async def _run_connection(self) -> None:
        async with connect(
            self.ws_url,
            ping_interval=None,
            open_timeout=10,
            close_timeout=5,
            max_queue=4096,
        ) as websocket:
            subscribed: dict[str, WalletSpec] = {}
            await self._sync_subscriptions(websocket, subscribed)
            await self._system("connection_open", self.ws_url)
            watcher = asyncio.create_task(
                self._watch_registry(websocket, subscribed),
                name="shadow-registry-watch",
            )
            heartbeat = asyncio.create_task(
                self._heartbeat(websocket),
                name="shadow-heartbeat",
            )
            try:
                async for raw_message in websocket:
                    received_at_ns = time.time_ns()
                    received_monotonic_ns = time.monotonic_ns()
                    try:
                        message = json.loads(raw_message)
                    except (json.JSONDecodeError, TypeError):
                        await self._system("invalid_json", str(raw_message)[:500])
                        continue
                    if not isinstance(message, dict):
                        continue
                    channel = message.get("channel")
                    if channel == "subscriptionResponse":
                        continue
                    if channel == "pong":
                        continue
                    if channel != "userFills":
                        continue
                    data = message.get("data")
                    if not isinstance(data, dict):
                        continue
                    user = str(data.get("user", "")).lower()
                    fills = data.get("fills")
                    if not isinstance(fills, list):
                        continue
                    wallet = subscribed.get(user)
                    for raw_fill in fills:
                        if not isinstance(raw_fill, dict):
                            continue
                        await self._record_fill(
                            user=user,
                            wallet=wallet,
                            fill=raw_fill,
                            is_snapshot=bool(data.get("isSnapshot", False)),
                            received_at_ns=received_at_ns,
                            received_monotonic_ns=received_monotonic_ns,
                        )
            finally:
                watcher.cancel()
                heartbeat.cancel()
                await asyncio.gather(watcher, heartbeat, return_exceptions=True)

    async def _record_fill(
        self,
        *,
        user: str,
        wallet: WalletSpec | None,
        fill: dict[str, Any],
        is_snapshot: bool,
        received_at_ns: int,
        received_monotonic_ns: int,
    ) -> None:
        try:
            exchange_ts_ms = int(fill["time"])
        except (KeyError, TypeError, ValueError):
            exchange_ts_ms = None
        if exchange_ts_ms is not None and exchange_ts_ms < self.started_ms:
            return
        if self.deduper.seen(user, fill):
            return
        observed_lag_ms = (
            received_at_ns / 1_000_000 - exchange_ts_ms
            if exchange_ts_ms is not None
            else None
        )
        coin = str(fill.get("coin", "")).upper()
        coverage = wallet is not None and (not wallet.coins or coin in wallet.coins)
        await self.sink.put(
            {
                "kind": "wallet_fill",
                "wallet_id": wallet.id if wallet else None,
                "wallet_label": wallet.label if wallet else None,
                "wallet_stage": wallet.stage if wallet else None,
                "wallet_address": user,
                "coin": coin,
                "exchange_ts_ms": exchange_ts_ms,
                "received_at_ns": received_at_ns,
                "received_monotonic_ns": received_monotonic_ns,
                "observed_event_lag_ms": observed_lag_ms,
                "is_snapshot": is_snapshot,
                "market_coin_configured": coverage,
                "fill": fill,
            }
        )
        if wallet is not None and wallet.coins and coin not in wallet.coins:
            await self._system(
                "uncovered_coin",
                f"{wallet.id}:{coin}; add coin to registry and restart market capture",
            )

    async def _watch_registry(self, websocket: Any, subscribed: dict[str, WalletSpec]) -> None:
        while True:
            await asyncio.sleep(self.reload_seconds)
            try:
                await self._sync_subscriptions(websocket, subscribed)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                await self._system("registry_reload_error", repr(exc))

    async def _sync_subscriptions(
        self,
        websocket: Any,
        subscribed: dict[str, WalletSpec],
    ) -> None:
        desired_specs = self.registry.active_hyperliquid_wallets()
        desired = {wallet.source_ref.lower(): wallet for wallet in desired_specs}
        for address in sorted(set(subscribed) - set(desired)):
            await websocket.send(
                json.dumps(
                    {
                        "method": "unsubscribe",
                        "subscription": {"type": "userFills", "user": address},
                    },
                    separators=(",", ":"),
                )
            )
            subscribed.pop(address, None)
            await self._system("wallet_unsubscribed", address)
        for address in sorted(set(desired) - set(subscribed)):
            await websocket.send(
                json.dumps(
                    {
                        "method": "subscribe",
                        "subscription": {
                            "type": "userFills",
                            "user": address,
                            "aggregateByTime": False,
                        },
                    },
                    separators=(",", ":"),
                )
            )
            subscribed[address] = desired[address]
            await self._system("wallet_subscribed", f"{desired[address].id}:{address}")
        for address in set(desired) & set(subscribed):
            subscribed[address] = desired[address]

    async def _heartbeat(self, websocket: Any) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            await websocket.send('{"method":"ping"}')

    async def _system(self, event: str, detail: str = "") -> None:
        await self.sink.put(
            {
                "kind": "system",
                "event": event,
                "detail": detail,
                "received_at_ns": time.time_ns(),
                "received_monotonic_ns": time.monotonic_ns(),
            }
        )


async def run_shadow_validation(
    *,
    ws_url: str,
    registry: WalletRegistry,
    shadow_dir: Path,
    market_dir: Path,
    extra_coins: tuple[str, ...] = (),
    market_flush_rows: int = 5_000,
    market_flush_seconds: float = 5.0,
    market_queue_size: int = 50_000,
    heartbeat_seconds: float = 30.0,
    reconnect_base_seconds: float = 1.0,
    reconnect_max_seconds: float = 30.0,
) -> None:
    registry.init()
    coins = tuple(dict.fromkeys((*extra_coins, *registry.market_coins())))
    fill_collector = HyperliquidWalletFillCollector(
        ws_url=ws_url,
        registry=registry,
        sink=JsonlShadowSink(shadow_dir),
        heartbeat_seconds=heartbeat_seconds,
        reconnect_base_seconds=reconnect_base_seconds,
        reconnect_max_seconds=reconnect_max_seconds,
    )
    tasks = [asyncio.create_task(fill_collector.run(), name="shadow-wallet-fills")]
    if coins:
        tasks.append(
            asyncio.create_task(
                capture_market(
                    ws_url=ws_url,
                    coins=coins,
                    output_dir=market_dir,
                    flush_rows=market_flush_rows,
                    flush_seconds=market_flush_seconds,
                    queue_size=market_queue_size,
                    heartbeat_seconds=heartbeat_seconds,
                    reconnect_base_seconds=reconnect_base_seconds,
                    reconnect_max_seconds=reconnect_max_seconds,
                ),
                name="shadow-market-capture",
            )
        )
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
