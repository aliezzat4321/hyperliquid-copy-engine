from __future__ import annotations

import json
import math
import time
from collections import deque
from collections.abc import Iterable
from typing import Any


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _lag_ms(exchange_ts_ms: int | None, received_at_ns: int) -> float | None:
    if exchange_ts_ms is None:
        return None
    return received_at_ns / 1_000_000 - exchange_ts_ms


def _level(level: Any) -> tuple[float | None, float | None, int | None]:
    if not isinstance(level, dict):
        return None, None, None
    return _float(level.get("px")), _float(level.get("sz")), _int(level.get("n"))


def _imbalance(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    total = bid + ask
    return None if total <= 0 else (bid - ask) / total


def _microprice(
    bid_px: float | None,
    bid_sz: float | None,
    ask_px: float | None,
    ask_sz: float | None,
) -> float | None:
    if None in {bid_px, bid_sz, ask_px, ask_sz}:
        return None
    assert bid_px is not None and bid_sz is not None
    assert ask_px is not None and ask_sz is not None
    total = bid_sz + ask_sz
    return None if total <= 0 else (ask_px * bid_sz + bid_px * ask_sz) / total


def _spread_bps(bid_px: float | None, ask_px: float | None) -> float | None:
    if bid_px is None or ask_px is None or ask_px < bid_px:
        return None
    mid = (bid_px + ask_px) / 2
    return None if mid <= 0 else (ask_px - bid_px) / mid * 10_000


def _depth_usd(
    levels: list[Any],
    *,
    mid_px: float,
    bps: float,
    is_bid: bool,
) -> float:
    cutoff = mid_px * (1 - bps / 10_000) if is_bid else mid_px * (1 + bps / 10_000)
    total = 0.0
    for raw_level in levels:
        px, sz, _ = _level(raw_level)
        if px is None or sz is None:
            continue
        inside = px >= cutoff if is_bid else px <= cutoff
        if inside:
            total += px * sz
    return total


def _base_row(
    channel: str,
    coin: str,
    exchange_ts_ms: int | None,
    received_at_ns: int,
    received_monotonic_ns: int,
    raw_data: Any,
) -> dict[str, Any]:
    return {
        "channel": channel,
        "coin": coin,
        "exchange_ts_ms": exchange_ts_ms,
        "received_at_ns": received_at_ns,
        "received_monotonic_ns": received_monotonic_ns,
        "observed_event_lag_ms": _lag_ms(exchange_ts_ms, received_at_ns),
        "raw_json": _json(raw_data),
    }


def _normalize_bbo(data: Any, received_at_ns: int, monotonic_ns: int) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    bbo = data.get("bbo")
    if not isinstance(bbo, list) or len(bbo) != 2:
        return []
    bid_px, bid_sz, bid_orders = _level(bbo[0])
    ask_px, ask_sz, ask_orders = _level(bbo[1])
    mid_px = None if bid_px is None or ask_px is None else (bid_px + ask_px) / 2
    exchange_ts_ms = _int(data.get("time"))
    row = _base_row(
        "bbo",
        str(data.get("coin", "")),
        exchange_ts_ms,
        received_at_ns,
        monotonic_ns,
        data,
    )
    row.update(
        {
            "bid_px": bid_px,
            "bid_sz": bid_sz,
            "bid_orders": bid_orders,
            "ask_px": ask_px,
            "ask_sz": ask_sz,
            "ask_orders": ask_orders,
            "mid_px": mid_px,
            "spread_bps": _spread_bps(bid_px, ask_px),
            "bbo_imbalance": _imbalance(bid_sz, ask_sz),
            "microprice": _microprice(bid_px, bid_sz, ask_px, ask_sz),
        }
    )
    return [row]


def _normalize_l2(data: Any, received_at_ns: int, monotonic_ns: int) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    levels = data.get("levels")
    if not isinstance(levels, list) or len(levels) != 2:
        return []
    bids = levels[0] if isinstance(levels[0], list) else []
    asks = levels[1] if isinstance(levels[1], list) else []
    best_bid_px, best_bid_sz, _ = _level(bids[0] if bids else None)
    best_ask_px, best_ask_sz, _ = _level(asks[0] if asks else None)
    mid_px = (
        None
        if best_bid_px is None or best_ask_px is None
        else (best_bid_px + best_ask_px) / 2
    )
    metrics: dict[str, float | None] = {}
    for bps in (5, 10):
        bid_depth = None
        ask_depth = None
        if mid_px is not None and mid_px > 0:
            bid_depth = _depth_usd(bids, mid_px=mid_px, bps=bps, is_bid=True)
            ask_depth = _depth_usd(asks, mid_px=mid_px, bps=bps, is_bid=False)
        metrics[f"bid_depth_usd_{bps}bps"] = bid_depth
        metrics[f"ask_depth_usd_{bps}bps"] = ask_depth
        metrics[f"depth_imbalance_{bps}bps"] = _imbalance(bid_depth, ask_depth)
    exchange_ts_ms = _int(data.get("time"))
    row = _base_row(
        "l2Book",
        str(data.get("coin", "")),
        exchange_ts_ms,
        received_at_ns,
        monotonic_ns,
        data,
    )
    row.update(
        {
            "bid_levels_json": _json(bids),
            "ask_levels_json": _json(asks),
            "best_bid_px": best_bid_px,
            "best_ask_px": best_ask_px,
            "mid_px": mid_px,
            "spread_bps": _spread_bps(best_bid_px, best_ask_px),
            "bbo_imbalance": _imbalance(best_bid_sz, best_ask_sz),
            "microprice": _microprice(
                best_bid_px,
                best_bid_sz,
                best_ask_px,
                best_ask_sz,
            ),
            **metrics,
        }
    )
    return [row]


def _normalize_trades(
    data: Any,
    received_at_ns: int,
    monotonic_ns: int,
) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    rows: list[dict[str, Any]] = []
    for trade in data:
        if not isinstance(trade, dict):
            continue
        side = str(trade.get("side", ""))
        px = _float(trade.get("px"))
        sz = _float(trade.get("sz"))
        notional = None if px is None or sz is None else px * sz
        sign = 1.0 if side == "B" else -1.0 if side == "A" else 0.0
        users = trade.get("users")
        buyer = str(users[0]) if isinstance(users, list) and len(users) > 0 else None
        seller = str(users[1]) if isinstance(users, list) and len(users) > 1 else None
        exchange_ts_ms = _int(trade.get("time"))
        row = _base_row(
            "trades",
            str(trade.get("coin", "")),
            exchange_ts_ms,
            received_at_ns,
            monotonic_ns,
            trade,
        )
        row.update(
            {
                "tid": _int(trade.get("tid")),
                "side": side,
                "px": px,
                "sz": sz,
                "notional_usd": notional,
                "signed_notional_usd": None if notional is None else sign * notional,
                "hash": str(trade.get("hash", "")),
                "buyer": buyer,
                "seller": seller,
            }
        )
        rows.append(row)
    return rows


def _normalize_asset_ctx(
    data: Any,
    received_at_ns: int,
    monotonic_ns: int,
) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("ctx"), dict):
        return []
    ctx = data["ctx"]
    row = _base_row(
        "activeAssetCtx",
        str(data.get("coin", "")),
        None,
        received_at_ns,
        monotonic_ns,
        data,
    )
    row.update(
        {
            "mark_px": _float(ctx.get("markPx")),
            "mid_px": _float(ctx.get("midPx")),
            "oracle_px": _float(ctx.get("oraclePx")),
            "funding": _float(ctx.get("funding")),
            "open_interest": _float(ctx.get("openInterest")),
            "day_notional_volume": _float(ctx.get("dayNtlVlm")),
            "prev_day_px": _float(ctx.get("prevDayPx")),
            "premium": _float(ctx.get("premium")),
        }
    )
    return [row]


def normalize_market_message(
    message: dict[str, Any],
    *,
    received_at_ns: int | None = None,
    received_monotonic_ns: int | None = None,
) -> list[dict[str, Any]]:
    received_at_ns = received_at_ns if received_at_ns is not None else time.time_ns()
    monotonic_ns = (
        received_monotonic_ns if received_monotonic_ns is not None else time.monotonic_ns()
    )
    channel = message.get("channel")
    data = message.get("data")
    if channel == "bbo":
        return _normalize_bbo(data, received_at_ns, monotonic_ns)
    if channel == "l2Book":
        return _normalize_l2(data, received_at_ns, monotonic_ns)
    if channel == "trades":
        return _normalize_trades(data, received_at_ns, monotonic_ns)
    if channel == "activeAssetCtx":
        return _normalize_asset_ctx(data, received_at_ns, monotonic_ns)
    return []


def system_record(event: str, detail: str = "") -> dict[str, Any]:
    received_at_ns = time.time_ns()
    return {
        **_base_row(
            "system",
            "_ALL",
            None,
            received_at_ns,
            time.monotonic_ns(),
            {"event": event, "detail": detail},
        ),
        "event": event,
        "detail": detail,
    }


def build_subscriptions(coins: Iterable[str]) -> list[dict[str, str]]:
    cleaned = dict.fromkeys(str(coin).upper().strip() for coin in coins if str(coin).strip())
    return [
        {"type": subscription_type, "coin": coin}
        for coin in cleaned
        for subscription_type in ("bbo", "l2Book", "trades", "activeAssetCtx")
    ]


class TradeDeduper:
    def __init__(self, max_keys: int = 100_000) -> None:
        self._max_keys = max_keys
        self._keys: set[tuple[str, int, int]] = set()
        self._order: deque[tuple[str, int, int]] = deque()

    def seen(self, row: dict[str, Any]) -> bool:
        if row.get("channel") != "trades":
            return False
        exchange_ts_ms = _int(row.get("exchange_ts_ms"))
        tid = _int(row.get("tid"))
        if exchange_ts_ms is None or tid is None:
            return False
        key = (str(row.get("coin", "")), exchange_ts_ms, tid)
        if key in self._keys:
            return True
        self._keys.add(key)
        self._order.append(key)
        while len(self._order) > self._max_keys:
            self._keys.discard(self._order.popleft())
        return False
