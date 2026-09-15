from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from hlcopy.config import Settings
from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.market.symbols import wire_coin


class FundingRangeEvent(Protocol):
    coin: str
    exchange_ts_ms: int


def funding_ranges_for_event_groups(
    event_groups: Iterable[Iterable[FundingRangeEvent]],
) -> dict[str, tuple[int, int]]:
    """Collapse candidate event spans into one official-funding fetch range per coin."""
    ranges: dict[str, tuple[int, int]] = {}
    for events in event_groups:
        for event in events:
            coin = event.coin
            ts_ms = int(event.exchange_ts_ms)
            existing = ranges.get(coin)
            if existing is None:
                ranges[coin] = (ts_ms, ts_ms)
            else:
                ranges[coin] = (min(existing[0], ts_ms), max(existing[1], ts_ms))
    return ranges


async def fetch_official_funding_history(
    ranges: dict[str, tuple[int, int]],
) -> tuple[dict[str, list[dict[str, object]]], dict[str, str]]:
    """Fetch finalized Hyperliquid funding once per coin, isolating per-coin failures."""
    if not ranges:
        return {}, {}
    settings = Settings.from_env()
    rows_by_coin: dict[str, list[dict[str, object]]] = {}
    errors: dict[str, str] = {}
    async with HyperliquidHttpClient(
        settings.api_url,
        settings.leaderboard_url,
        concurrency=settings.http_concurrency,
    ) as client:
        for coin, (start_ms, end_ms) in sorted(ranges.items()):
            try:
                pages = await client.funding_history_by_time(
                    wire_coin(coin),
                    start_ms,
                    end_ms,
                )
                rows: list[dict[str, object]] = []
                for page in pages:
                    payload = page.response_payload
                    if not isinstance(payload, list):
                        continue
                    rows.extend(row for row in payload if isinstance(row, dict))
                rows_by_coin[coin] = rows
            except Exception as exc:
                # One data dependency must not stop unrelated coins. Downstream scoring
                # treats this coin as unevaluable rather than profitable or unprofitable.
                errors[coin] = f"{type(exc).__name__}: {exc}"
    return rows_by_coin, errors


def required_history_rows(
    history_rows: Iterable[dict[str, object]],
    required_boundaries: Iterable[tuple[str, int]],
) -> list[dict[str, object]]:
    """Keep only finalized funding rows that a simulated completed episode requires."""
    required_times = {boundary for _coin, boundary in required_boundaries}
    selected: list[dict[str, object]] = []
    for row in history_rows:
        try:
            time_ms = int(row.get("time", -1))
        except (TypeError, ValueError):
            continue
        if time_ms in required_times:
            selected.append(row)
    return selected
