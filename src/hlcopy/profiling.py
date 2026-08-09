from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from hlcopy.analytics.trader_profile import TraderProfile, build_trader_profile
from hlcopy.config import Settings
from hlcopy.db.postgres import Database
from hlcopy.discovery.leaderboard import LeaderboardCandidate, parse_leaderboard
from hlcopy.hyperliquid.http_client import ApiResponse, HyperliquidHttpClient
from hlcopy.models import Fill
from hlcopy.positions.reconstruction import reconstruct_positions


def _is_perp_fill(row: dict[str, Any]) -> bool:
    direction = str(row.get("dir", ""))
    return "Long" in direction or "Short" in direction


def _merge_fill_pages(pages: list[ApiResponse]) -> list[dict[str, Any]]:
    rows: dict[tuple[int, int, str], dict[str, Any]] = {}
    for page in pages:
        payload = page.response_payload
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, dict) or not _is_perp_fill(row):
                continue
            try:
                key = (int(row["time"]), int(row["tid"]), str(row.get("hash", "")))
            except (KeyError, TypeError, ValueError):
                continue
            rows.setdefault(key, row)
    merged = list(rows.values())
    # Pages are fetched forward in time. Sort only on exchange timestamp so Python's
    # stable sort preserves first-seen API order among fills sharing that millisecond.
    merged.sort(key=lambda row: int(row["time"]))
    return merged


def _fill_history_cap_hit(pages: list[ApiResponse]) -> bool:
    keys: set[tuple[int, int, str]] = set()
    for page in pages:
        payload = page.response_payload
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, dict):
                continue
            try:
                keys.add((int(row["time"]), int(row["tid"]), str(row.get("hash", ""))))
            except (KeyError, TypeError, ValueError):
                continue
    return len(keys) >= 10_000


def _merge_funding_pages(pages: list[ApiResponse]) -> list[dict[str, Any]]:
    rows: dict[tuple[int, str, str, str], dict[str, Any]] = {}
    for page in pages:
        payload = page.response_payload
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, dict):
                continue
            delta = row.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            key = (
                int(row.get("time", 0)),
                str(delta.get("coin", "")),
                str(delta.get("usdc", "")),
                str(row.get("hash", "")),
            )
            rows[key] = row
    return [rows[key] for key in sorted(rows)]


def _leaderboard_metrics(candidate: LeaderboardCandidate) -> dict[str, object]:
    result: dict[str, object] = {"account_value": candidate.account_value}
    for window in ("day", "week", "month", "allTime"):
        stats = candidate.window(window)
        prefix = "all_time" if window == "allTime" else window
        result[f"{prefix}_pnl"] = stats.pnl
        result[f"{prefix}_roi"] = stats.roi
        result[f"{prefix}_volume"] = stats.volume
    return result


def _filter_orders(
    payload: Any,
    *,
    lookback_start_ms: int,
    perp_coins: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    result: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        order = row.get("order") or {}
        if not isinstance(order, dict):
            continue
        timestamp = int(row.get("statusTimestamp", order.get("timestamp", 0)) or 0)
        if timestamp < lookback_start_ms:
            continue
        if perp_coins and str(order.get("coin", "")) not in perp_coins:
            continue
        result.append(row)
    return result


def _filter_twap_rows(
    payload: Any,
    *,
    lookback_start_ms: int,
    perp_coins: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    result: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        fill = row.get("fill") or {}
        if not isinstance(fill, dict) or not _is_perp_fill(fill):
            continue
        if int(fill.get("time", 0) or 0) < lookback_start_ms:
            continue
        if perp_coins and str(fill.get("coin", "")) not in perp_coins:
            continue
        result.append(row)
    return result


async def _store_response(db: Database, response: ApiResponse) -> None:
    request = response.request_payload
    endpoint = str(request.get("type")) if isinstance(request, dict) else response.endpoint
    await db.store_raw(
        source="hyperliquid",
        endpoint=endpoint,
        request_payload=request,
        response_payload=response.response_payload,
        fetched_at_ms=response.fetched_at_ms,
    )


async def _optional_sources(
    client: HyperliquidHttpClient,
    wallet: str,
) -> tuple[dict[str, Any], dict[str, bool], list[ApiResponse]]:
    names = (
        "clearinghouse_state",
        "portfolio",
        "historical_orders",
        "twap_slice_fills",
        "user_role",
        "user_abstraction",
        "user_fees",
    )
    calls = (
        client.clearinghouse_state(wallet),
        client.portfolio(wallet),
        client.historical_orders(wallet),
        client.user_twap_slice_fills(wallet),
        client.user_role(wallet),
        client.user_abstraction(wallet),
        client.user_fees(wallet),
    )
    responses = await asyncio.gather(*calls, return_exceptions=True)
    payloads: dict[str, Any] = {}
    status: dict[str, bool] = {}
    successful: list[ApiResponse] = []
    for name, result in zip(names, responses, strict=True):
        if isinstance(result, BaseException):
            status[name] = False
            payloads[name] = None
            print(f"  optional source unavailable {name}: {result}", flush=True)
            continue
        status[name] = True
        payloads[name] = result.response_payload
        successful.append(result)
    return payloads, status, successful


async def run_trader_profiles(
    settings: Settings,
    *,
    limit: int | None = None,
    lookback_days: int | None = None,
) -> Path:
    selected_limit = limit or settings.profile_candidates
    selected_lookback = lookback_days or settings.profile_lookback_days
    as_of_ms = int(time.time() * 1000)
    lookback_start_ms = as_of_ms - selected_lookback * 86_400_000
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    profiles: list[TraderProfile] = []

    async with Database(settings.database_url) as db:
        await db.init_schema()
        async with HyperliquidHttpClient(
            settings.api_url,
            settings.leaderboard_url,
            concurrency=settings.http_concurrency,
        ) as client:
            leaderboard = await client.leaderboard()
            await _store_response(db, leaderboard)
            candidates = parse_leaderboard(leaderboard.response_payload)
            await db.upsert_leaderboard(candidates, leaderboard.fetched_at_ms)
            selected = candidates[:selected_limit]

            for rank, candidate in enumerate(selected, start=1):
                wallet = candidate.address
                print(f"[{rank}/{len(selected)}] profiling {wallet}", flush=True)
                try:
                    fill_pages = await client.user_fills_by_time(
                        wallet,
                        lookback_start_ms,
                        as_of_ms,
                    )
                    for page in fill_pages:
                        await _store_response(db, page)
                    raw_fills = _merge_fill_pages(fill_pages)
                    fills = [Fill.from_raw(wallet, row) for row in raw_fills]
                    fills.sort(key=lambda fill: fill.timestamp_ms)
                    await db.upsert_fills(fills)
                    episodes, _states = reconstruct_positions(fills) if fills else ([], {})
                    await db.replace_episodes(wallet, episodes)
                except Exception as exc:
                    print(f"  data-quality rejection: {exc}", flush=True)
                    continue

                funding_pages: list[ApiResponse] = []
                funding_available = True
                try:
                    funding_pages = await client.user_funding_by_time(
                        wallet,
                        lookback_start_ms,
                        as_of_ms,
                    )
                    for page in funding_pages:
                        await _store_response(db, page)
                except Exception as exc:
                    funding_available = False
                    print(f"  optional source unavailable funding: {exc}", flush=True)
                funding_rows = _merge_funding_pages(funding_pages)

                payloads, source_status, successful = await _optional_sources(client, wallet)
                source_status["funding"] = funding_available
                for response in successful:
                    await _store_response(db, response)

                perp_coins = {fill.coin for fill in fills}
                raw_orders = payloads.get("historical_orders")
                raw_twaps = payloads.get("twap_slice_fills")
                orders = _filter_orders(
                    raw_orders,
                    lookback_start_ms=lookback_start_ms,
                    perp_coins=perp_coins,
                )
                twap_rows = _filter_twap_rows(
                    raw_twaps,
                    lookback_start_ms=lookback_start_ms,
                    perp_coins=perp_coins,
                )
                order_limit_hit = isinstance(raw_orders, list) and len(raw_orders) >= 2_000
                twap_limit_hit = isinstance(raw_twaps, list) and len(raw_twaps) >= 2_000
                profile = build_trader_profile(
                    wallet_address=wallet,
                    leaderboard_rank=rank,
                    display_name=candidate.display_name,
                    as_of_ms=as_of_ms,
                    lookback_start_ms=lookback_start_ms,
                    leaderboard_metrics=_leaderboard_metrics(candidate),
                    fills=fills,
                    episodes=episodes,
                    clearinghouse_state=payloads.get("clearinghouse_state"),
                    portfolio=payloads.get("portfolio"),
                    historical_orders=orders,
                    twap_slice_fills=twap_rows,
                    funding_rows=funding_rows,
                    user_role=payloads.get("user_role"),
                    user_abstraction=payloads.get("user_abstraction"),
                    user_fees=payloads.get("user_fees"),
                    history_cap_hit=_fill_history_cap_hit(fill_pages),
                    historical_order_limit_hit=order_limit_hit,
                    twap_slice_limit_hit=twap_limit_hit,
                    source_status=source_status,
                )
                profiles.append(profile)
                await db.store_trader_profile(profile)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = settings.output_dir / f"trader_profiles_{stamp}"
    json_path = base.with_suffix(".json")
    csv_path = base.with_suffix(".csv")
    parquet_path = base.with_suffix(".parquet")
    json_path.write_text(
        json.dumps(
            [profile.to_dict() for profile in profiles],
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    rows = [profile.to_flat_dict() for profile in profiles]
    frame = pl.DataFrame(rows) if rows else pl.DataFrame({"wallet_address": []})
    frame.write_csv(csv_path)
    frame.write_parquet(parquet_path)
    print(f"wrote {json_path}")
    print(f"wrote {csv_path}")
    print(f"wrote {parquet_path}")
    return json_path


def run(
    settings: Settings | None = None,
    *,
    limit: int | None = None,
    lookback_days: int | None = None,
) -> Path:
    return asyncio.run(
        run_trader_profiles(
            settings or Settings.from_env(),
            limit=limit,
            lookback_days=lookback_days,
        )
    )
