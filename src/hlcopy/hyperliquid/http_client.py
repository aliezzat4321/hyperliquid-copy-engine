from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx


class HyperliquidError(RuntimeError):
    pass


class WeightedRateLimiter:
    """Conservative rolling-window limiter for Hyperliquid's weighted REST budget."""

    def __init__(self, capacity: int = 1_100, window_seconds: float = 60.0) -> None:
        self.capacity = capacity
        self.window_seconds = window_seconds
        self._events: list[tuple[float, int]] = []
        self._lock = asyncio.Lock()

    async def consume(self, weight: int) -> None:
        if weight <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self.window_seconds
                self._events = [(ts, w) for ts, w in self._events if ts > cutoff]
                used = sum(w for _, w in self._events)
                if used + weight <= self.capacity:
                    self._events.append((now, weight))
                    return
                sleep_for = max(0.05, self._events[0][0] + self.window_seconds - now)
            await asyncio.sleep(sleep_for)


@dataclass(frozen=True, slots=True)
class ApiResponse:
    endpoint: str
    request_payload: dict[str, Any] | None
    response_payload: Any
    fetched_at_ms: int


class HyperliquidHttpClient:
    def __init__(
        self,
        api_url: str,
        leaderboard_url: str,
        *,
        timeout_seconds: float = 20.0,
        concurrency: int = 3,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.leaderboard_url = leaderboard_url
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "hyperliquid-copy-engine/0.1"},
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._limiter = WeightedRateLimiter()

    async def __aenter__(self) -> "HyperliquidHttpClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        weight: int = 1,
        retries: int = 4,
    ) -> Any:
        async with self._sem:
            for attempt in range(retries + 1):
                await self._limiter.consume(weight)
                try:
                    response = await self._client.request(method, url, json=json)
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt == retries:
                            response.raise_for_status()
                        await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
                        continue
                    response.raise_for_status()
                    return response.json()
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt == retries:
                        raise HyperliquidError(f"request failed after retries: {url}") from exc
                    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
        raise AssertionError("unreachable")

    async def leaderboard(self) -> ApiResponse:
        payload = await self._request_json("GET", self.leaderboard_url, weight=1)
        return ApiResponse("leaderboard", None, payload, int(time.time() * 1000))

    async def info(self, payload: dict[str, Any], *, weight: int = 20) -> ApiResponse:
        data = await self._request_json(
            "POST", f"{self.api_url}/info", json=payload, weight=weight
        )
        if isinstance(data, list) and payload.get("type") in {
            "historicalOrders",
            "userFills",
            "userFillsByTime",
            "userFunding",
            "fundingHistory",
            "userTwapSliceFills",
        }:
            await self._limiter.consume(math.ceil(len(data) / 20))
        return ApiResponse("info", payload, data, int(time.time() * 1000))

    async def user_fills(self, user: str) -> ApiResponse:
        return await self.info({"type": "userFills", "user": user, "aggregateByTime": False})

    async def user_fills_by_time(
        self, user: str, start_time_ms: int, end_time_ms: int | None = None
    ) -> list[ApiResponse]:
        """Paginate time-range fills without assuming one response is complete.

        Hyperliquid only exposes the most recent 10k fills through this endpoint. The caller
        must treat a non-flat starting position as truncated history during reconstruction.
        """
        pages: list[ApiResponse] = []
        cursor = start_time_ms
        seen: set[tuple[int, int, str | None]] = set()
        for _ in range(40):
            request: dict[str, Any] = {
                "type": "userFillsByTime",
                "user": user,
                "startTime": cursor,
                "aggregateByTime": False,
            }
            if end_time_ms is not None:
                request["endTime"] = end_time_ms
            page = await self.info(request)
            pages.append(page)
            rows = page.response_payload
            if not isinstance(rows, list) or not rows:
                break
            fresh = []
            for row in rows:
                key = (int(row["time"]), int(row["tid"]), row.get("hash"))
                if key not in seen:
                    seen.add(key)
                    fresh.append(row)
            if not fresh:
                break
            last_time = max(int(row["time"]) for row in rows)
            if end_time_ms is not None and last_time >= end_time_ms:
                break
            if last_time < cursor:
                raise HyperliquidError("userFillsByTime pagination moved backwards")
            cursor = last_time
            if len(seen) >= 10_000:
                break
        return pages

    async def user_funding(
        self, user: str, start_time_ms: int, end_time_ms: int | None = None
    ) -> ApiResponse:
        payload: dict[str, Any] = {
            "type": "userFunding",
            "user": user,
            "startTime": start_time_ms,
        }
        if end_time_ms is not None:
            payload["endTime"] = end_time_ms
        return await self.info(payload)

    async def clearinghouse_state(self, user: str) -> ApiResponse:
        return await self.info({"type": "clearinghouseState", "user": user}, weight=2)

    async def l2_book(self, coin: str) -> ApiResponse:
        return await self.info({"type": "l2Book", "coin": coin}, weight=2)
