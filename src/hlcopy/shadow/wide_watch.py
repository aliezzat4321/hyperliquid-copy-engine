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

from hlcopy.market.symbols import canonical_coin, wire_coin
from hlcopy.shadow.capture import load_market_coin_file
from hlcopy.shadow.registry import WalletRegistry, WalletSpec

logger = logging.getLogger(__name__)

TRACKED_STAGES = frozenset({"research", "validation", "approved"})


class JsonlWideTradeSink:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = asyncio.Lock()

    async def put(self, row: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._append, row)

    def _append(self, row: dict[str, Any]) -> None:
        timestamp_ns = int(row.get("received_at_ns") or time.time_ns())
        day = time.strftime("%Y-%m-%d", time.gmtime(timestamp_ns / 1_000_000_000))
        path = self.root / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
            handle.flush()


class _TargetTradeDeduper:
    def __init__(self, max_keys: int = 300_000) -> None:
        self.max_keys = max_keys
        self.keys: set[tuple[str, str, int, int]] = set()
        self.order: deque[tuple[str, str, int, int]] = deque()

    def seen(self, address: str, coin: str, trade: dict[str, Any]) -> bool:
        try:
            key = (
                address.lower(),
                coin,
                int(trade["time"]),
                int(trade["tid"]),
            )
        except (KeyError, TypeError, ValueError):
            return False
        if key in self.keys:
            return True
        self.keys.add(key)
        self.order.append(key)
        while len(self.order) > self.max_keys:
            self.keys.discard(self.order.popleft())
        return False


def tracked_wallet_map(registry: WalletRegistry) -> dict[str, WalletSpec]:
    return {
        wallet.source_ref.lower(): wallet
        for wallet in registry.load()
        if wallet.enabled
        and wallet.source_type == "hyperliquid_wallet"
        and wallet.stage in TRACKED_STAGES
    }


def _subscription_coins(path: Path) -> tuple[str, ...]:
    coins = load_market_coin_file(path)
    return tuple(dict.fromkeys(wire_coin(coin) for coin in coins if wire_coin(coin)))


class HyperliquidWideTradeCollector:
    """Watch public market trades and retain only rows touching tracked wallets.

    Hyperliquid caps user-specific websocket subscriptions at 10 unique users per IP.
    Public ``trades`` subscriptions are market-specific instead. Each trade contains
    buyer/seller addresses, so one public market stream can prospectively observe a
    much larger research wallet set without consuming additional user-specific slots.
    """

    def __init__(
        self,
        *,
        ws_url: str,
        registry: WalletRegistry,
        coins_file: Path,
        sink: JsonlWideTradeSink,
        reload_seconds: float = 5.0,
        heartbeat_seconds: float = 30.0,
        reconnect_base_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
    ) -> None:
        self.ws_url = ws_url
        self.registry = registry
        self.coins_file = coins_file
        self.sink = sink
        self.reload_seconds = max(1.0, reload_seconds)
        self.heartbeat_seconds = max(5.0, heartbeat_seconds)
        self.reconnect_base_seconds = max(0.1, reconnect_base_seconds)
        self.reconnect_max_seconds = max(self.reconnect_base_seconds, reconnect_max_seconds)
        self.deduper = _TargetTradeDeduper()

    async def run(self) -> None:
        attempt = 0
        while True:
            coins = _subscription_coins(self.coins_file)
            if not coins:
                raise ValueError("wide trade watcher requires at least one market coin")
            try:
                await self._run_connection(coins)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except (WebSocketException, OSError, TimeoutError) as exc:
                await self._system("connection_lost", type(exc).__name__)
                logger.warning("wide public trade websocket lost: %s", type(exc).__name__)
            attempt += 1
            delay = min(
                self.reconnect_max_seconds,
                self.reconnect_base_seconds * (2 ** min(attempt - 1, 6)),
            )
            delay += random.uniform(0, delay * 0.25)
            await self._system("reconnect_wait", f"{delay:.3f}s")
            await asyncio.sleep(delay)

    async def _run_connection(self, coins: tuple[str, ...]) -> None:
        tracked = tracked_wallet_map(self.registry)
        next_reload = time.monotonic() + self.reload_seconds
        async with connect(
            self.ws_url,
            ping_interval=None,
            open_timeout=10,
            close_timeout=5,
            max_queue=8192,
        ) as websocket:
            await self._system(
                "connection_open",
                f"markets={len(coins)} tracked_wallets={len(tracked)}",
            )
            logger.info(
                "wide public trade watcher connected: markets=%d tracked_wallets=%d",
                len(coins),
                len(tracked),
            )
            for coin in coins:
                await websocket.send(
                    json.dumps(
                        {
                            "method": "subscribe",
                            "subscription": {"type": "trades", "coin": coin},
                        },
                        separators=(",", ":"),
                    )
                )
            heartbeat = asyncio.create_task(self._heartbeat(websocket), name="wide-trade-heartbeat")
            try:
                async for raw_message in websocket:
                    now = time.monotonic()
                    if now >= next_reload:
                        new_coins = _subscription_coins(self.coins_file)
                        if new_coins != coins:
                            await self._system(
                                "market_universe_changed",
                                f"old={len(coins)} new={len(new_coins)}",
                            )
                            return
                        refreshed = tracked_wallet_map(self.registry)
                        if set(refreshed) != set(tracked):
                            await self._system(
                                "watchlist_changed",
                                f"old={len(tracked)} new={len(refreshed)}",
                            )
                        tracked = refreshed
                        next_reload = now + self.reload_seconds

                    received_at_ns = time.time_ns()
                    received_monotonic_ns = time.monotonic_ns()
                    try:
                        message = json.loads(raw_message)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(message, dict):
                        continue
                    channel = message.get("channel")
                    if channel in {"subscriptionResponse", "pong"}:
                        continue
                    if channel != "trades":
                        continue
                    data = message.get("data")
                    if not isinstance(data, list):
                        continue
                    for trade in data:
                        if not isinstance(trade, dict):
                            continue
                        await self._record_trade(
                            trade=trade,
                            tracked=tracked,
                            received_at_ns=received_at_ns,
                            received_monotonic_ns=received_monotonic_ns,
                        )
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _record_trade(
        self,
        *,
        trade: dict[str, Any],
        tracked: dict[str, WalletSpec],
        received_at_ns: int,
        received_monotonic_ns: int,
    ) -> None:
        users = trade.get("users")
        if not isinstance(users, list) or len(users) < 2:
            return
        buyer = str(users[0]).lower()
        seller = str(users[1]).lower()
        matches: list[tuple[str, str]] = []
        if buyer in tracked:
            matches.append((buyer, "BUY"))
        if seller in tracked and seller != buyer:
            matches.append((seller, "SELL"))
        if not matches:
            return

        coin = canonical_coin(trade.get("coin", ""))
        if not coin:
            return
        try:
            exchange_ts_ms = int(trade["time"])
            tid = int(trade["tid"])
        except (KeyError, TypeError, ValueError):
            return
        observed_lag_ms = received_at_ns / 1_000_000 - exchange_ts_ms

        for address, target_side in matches:
            if self.deduper.seen(address, coin, trade):
                continue
            wallet = tracked[address]
            await self.sink.put(
                {
                    "kind": "public_wallet_trade",
                    "wallet_id": wallet.id,
                    "wallet_label": wallet.label,
                    "wallet_stage": wallet.stage,
                    "wallet_address": address,
                    "coin": coin,
                    "target_side": target_side,
                    "exchange_ts_ms": exchange_ts_ms,
                    "received_at_ns": received_at_ns,
                    "received_monotonic_ns": received_monotonic_ns,
                    "observed_event_lag_ms": observed_lag_ms,
                    "tid": tid,
                    "hash": str(trade.get("hash", "")),
                    "px": str(trade.get("px", "")),
                    "sz": str(trade.get("sz", "")),
                    "aggressor_side": str(trade.get("side", "")),
                    "buyer": buyer,
                    "seller": seller,
                    "raw_trade": trade,
                }
            )

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
