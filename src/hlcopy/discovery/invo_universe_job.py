from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hlcopy.discovery.invo_evidence import closed_trade_evidence
from hlcopy.discovery.invo_miner_job import _page_items
from hlcopy.discovery.invo_resolution_queue import materialize_resolution_queue_from_store
from hlcopy.discovery.invo_source import (
    InvoApiError,
    InvoReadOnlyClient,
    portfolio_candidates,
    verified_trade_events,
)
from hlcopy.discovery.invo_store import InvoRecordStore

DEFAULT_STATE_DIR = Path("/var/lib/hyperliquid-copy-engine/invo")
PORTFOLIO_FILTERS = ("trending", "day", "week", "month", "year", "all")
FILTER_LABELS = {
    "trending": "CROWN",
    "day": "1D",
    "week": "1W",
    "month": "1M",
    "year": "1Y",
    "all": "AT",
}
FEED_FILTERS = ("trending", "following", "all")
EVIDENCE_THRESHOLD = 20
PROFILE_REFRESH_SECONDS = 24 * 60 * 60


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exhaustive read-only Invo universe miner")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--portfolio-pages", type=int, default=10)
    parser.add_argument("--feed-pages", type=int, default=8)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--profile-limit", type=int, default=250)
    parser.add_argument("--profile-concurrency", type=int, default=4)
    return parser.parse_args()


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default)
    return value if isinstance(value, dict) else dict(default)


def _save_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _owner(post: Mapping[str, Any]) -> tuple[str, str]:
    update = post.get("update") if isinstance(post.get("update"), Mapping) else {}
    update_owner = update.get("owner") if isinstance(update.get("owner"), Mapping) else {}
    post_owner = post.get("owner") if isinstance(post.get("owner"), Mapping) else {}
    row = update_owner or post_owner
    return str(row.get("id") or "").strip(), str(row.get("username") or "").strip()


def _social_row(post: Mapping[str, Any], *, filter_name: str) -> dict[str, object] | None:
    post_id = str(post.get("id") or "").strip()
    owner_id, username = _owner(post)
    if not post_id or not owner_id:
        return None
    update = post.get("update") if isinstance(post.get("update"), Mapping) else {}
    portfolio = update.get("portfolio") if isinstance(update.get("portfolio"), Mapping) else {}
    return {
        "post_id": post_id,
        "owner_id": owner_id,
        "username": username,
        "portfolio_id": str(portfolio.get("id") or "").strip(),
        "feed_filter": filter_name,
        "post_type": str(post.get("postTypeId") or ""),
        "likes": int(post.get("likes") or 0),
        "comments": int(post.get("commentCount") or 0),
        "reposts": int(post.get("repostCount") or 0),
        "created_at": post.get("createdAt"),
        "verified_trade": bool(update.get("verifiedTrade", False)),
    }


def _merge_portfolio(
    target: dict[str, dict[str, object]],
    row: Mapping[str, object],
    *,
    surface: str,
) -> None:
    portfolio_id = str(row.get("portfolio_id") or "").strip()
    owner_id = str(row.get("owner_id") or "").strip()
    if not portfolio_id or not owner_id:
        return
    existing = target.get(portfolio_id)
    if existing is None:
        existing = dict(row)
        existing["surfaces"] = set()
        target[portfolio_id] = existing
    else:
        for key, value in row.items():
            if key in {"portfolio_id", "owner_id"}:
                continue
            if value not in (None, "", 0, 0.0, False):
                existing[key] = value
    surfaces = existing.setdefault("surfaces", set())
    if isinstance(surfaces, set):
        surfaces.add(surface)


async def _scan_portfolio_filter(
    client: InvoReadOnlyClient,
    *,
    filter_name: str,
    pages: int,
    page_size: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for page_number in range(1, max(1, pages) + 1):
        payload = await client.discover_portfolios(
            filter_name=filter_name,
            page=page_number,
            size=max(1, page_size),
        )
        page = portfolio_candidates(payload)
        if not page:
            break
        new_count = 0
        for candidate in page:
            row = candidate.to_dict()
            portfolio_id = str(row.get("portfolio_id") or "")
            if not portfolio_id or portfolio_id in seen:
                continue
            seen.add(portfolio_id)
            rows.append(row)
            new_count += 1
        if new_count == 0 or len(page) < max(1, page_size):
            break
    return rows


async def _scan_feed(
    client: InvoReadOnlyClient,
    *,
    filter_name: str,
    pages: int,
    page_size: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    social: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(max(1, pages)):
        payload = await client.feed(
            filter_name=filter_name,
            last_post_id=cursor,
            item_limit=max(1, page_size),
        )
        page = _page_items(payload)
        if not page:
            break
        new_count = 0
        for post in page:
            post_id = str(post.get("id") or "").strip()
            if not post_id or post_id in seen:
                continue
            seen.add(post_id)
            new_count += 1
            row = _social_row(post, filter_name=filter_name)
            if row is not None:
                social.append(row)
        for event in verified_trade_events({"items": page}):
            row = event.to_dict()
            evidence = closed_trade_evidence(event)
            if evidence is not None:
                row["resolver_evidence"] = evidence
            events.append(row)
        next_cursor = str(page[-1].get("id") or "").strip() or None
        if new_count == 0 or next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor
    return social, events


async def _profile_portfolios(
    client: InvoReadOnlyClient,
    *,
    owner_id: str,
    page_size: int,
    semaphore: asyncio.Semaphore,
) -> tuple[str, list[dict[str, object]], str | None]:
    async with semaphore:
        try:
            payload = await client.discover_portfolios(
                filter_name="user",
                page=1,
                size=max(10, page_size),
                user_id=owner_id,
            )
        except InvoApiError as exc:
            return owner_id, [], str(exc)
    return owner_id, [candidate.to_dict() for candidate in portfolio_candidates(payload)], None


def _owner_metrics(social_rows: list[dict[str, object]]) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = defaultdict(
        lambda: {"social_posts": 0, "verified_trade_posts": 0}
    )
    for row in social_rows:
        owner_id = str(row.get("owner_id") or "").strip()
        if not owner_id:
            continue
        output[owner_id]["social_posts"] += 1
        output[owner_id]["verified_trade_posts"] += int(bool(row.get("verified_trade")))
    return dict(output)


def _screen_score(
    row: Mapping[str, object],
    *,
    evidence_count: int,
    owner_stats: Mapping[str, int],
) -> float:
    closed = int(row.get("closed_positions") or 0)
    win_rate = float(row.get("win_rate") or 0.0)
    percent_change = float(row.get("percent_change") or 0.0)
    followers = int(row.get("follower_count") or 0)
    surfaces = row.get("surfaces")
    surface_count = len(surfaces) if isinstance(surfaces, set) else 0
    return round(
        min(closed, 1000) * 0.15
        + min(max(win_rate, 0.0), 100.0)
        + min(max(percent_change, 0.0), 10000.0) * 0.01
        + min(followers, 20000) * 0.001
        + min(owner_stats.get("social_posts", 0), 100) * 0.5
        + min(owner_stats.get("verified_trade_posts", 0), 100) * 1.5
        + min(evidence_count, 100) * 2.0
        + surface_count * 12.0
        - (100.0 if bool(row.get("liquidated", False)) else 0.0),
        3,
    )


async def run_once(args: argparse.Namespace) -> dict[str, object]:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise RuntimeError("Invo universe miner refuses REAL_TRADING_ENABLED=YES")
    access_token = os.getenv("INVO_ACCESS_TOKEN")
    refresh_token = os.getenv("INVO_REFRESH_TOKEN")
    if not access_token and not refresh_token:
        raise RuntimeError("Invo authentication is missing")

    state_dir: Path = args.state_dir
    store_path = state_dir / "archive.sqlite3"
    queue_dir = state_dir / "resolution_queue"
    universe_path = state_dir / "universe_candidates.json"
    cache_path = state_dir / "profile_portfolios_cache.json"
    previous = _load_json(universe_path, {})
    profile_cache = _load_json(cache_path, {})
    errors: list[str] = []
    portfolios: dict[str, dict[str, object]] = {}
    social_rows: list[dict[str, object]] = []
    trade_events: list[dict[str, object]] = []
    owner_priority: dict[str, int] = defaultdict(int)
    now_s = int(time.time())

    async with InvoReadOnlyClient(
        access_token=access_token,
        refresh_token=refresh_token,
        timeout_seconds=12.0,
        retry_attempts=2,
    ) as client:
        for filter_name in PORTFOLIO_FILTERS:
            try:
                rows = await _scan_portfolio_filter(
                    client,
                    filter_name=filter_name,
                    pages=max(1, args.portfolio_pages),
                    page_size=max(1, args.page_size),
                )
            except InvoApiError as exc:
                errors.append(f"leaderboard:{filter_name}:{exc}")
                continue
            for row in rows:
                _merge_portfolio(
                    portfolios,
                    row,
                    surface=f"leaderboard:{FILTER_LABELS[filter_name]}",
                )
                owner_id = str(row.get("owner_id") or "")
                owner_priority[owner_id] += 10

        for page_number in range(1, 5):
            try:
                payload = await client.trending_users(page=page_number, size=50)
            except InvoApiError as exc:
                errors.append(f"trending_users:{page_number}:{exc}")
                break
            page = _page_items(payload)
            if not page:
                break
            for row in page:
                if not isinstance(row, Mapping):
                    continue
                owner_id = str(row.get("id") or row.get("userId") or "").strip()
                if owner_id:
                    owner_priority[owner_id] += 8

        for filter_name in FEED_FILTERS:
            try:
                social, events = await _scan_feed(
                    client,
                    filter_name=filter_name,
                    pages=max(1, args.feed_pages),
                    page_size=max(1, args.page_size),
                )
            except InvoApiError as exc:
                errors.append(f"feed:{filter_name}:{exc}")
                continue
            social_rows.extend(social)
            trade_events.extend(events)
            for row in social:
                owner_id = str(row.get("owner_id") or "").strip()
                if owner_id:
                    owner_priority[owner_id] += 2 + 3 * int(bool(row.get("verified_trade")))

        owner_ids = {owner_id for owner_id in owner_priority if owner_id}
        owner_ids.update(
            str(row.get("owner_id") or "").strip()
            for row in portfolios.values()
            if str(row.get("owner_id") or "").strip()
        )
        ordered_owners = sorted(owner_ids, key=lambda value: (-owner_priority[value], value))
        ordered_owners = ordered_owners[: max(0, args.profile_limit)]
        semaphore = asyncio.Semaphore(max(1, args.profile_concurrency))
        tasks = []
        for owner_id in ordered_owners:
            cached = profile_cache.get(owner_id)
            cached_at = (
                int(cached.get("fetched_at_s", 0))
                if isinstance(cached, Mapping)
                else 0
            )
            cached_rows = (
                cached.get("portfolios", []) if isinstance(cached, Mapping) else []
            )
            cache_fresh = cached_at and now_s - cached_at < PROFILE_REFRESH_SECONDS
            if cache_fresh and isinstance(cached_rows, list):
                for row in cached_rows:
                    if isinstance(row, Mapping):
                        _merge_portfolio(portfolios, row, surface="profile:user")
                continue
            tasks.append(
                _profile_portfolios(
                    client,
                    owner_id=owner_id,
                    page_size=max(50, args.page_size),
                    semaphore=semaphore,
                )
            )
        if tasks:
            for owner_id, rows, error in await asyncio.gather(*tasks):
                if error is not None:
                    errors.append(f"profile:{owner_id}:{error}")
                    continue
                profile_cache[owner_id] = {"fetched_at_s": now_s, "portfolios": rows}
                for row in rows:
                    _merge_portfolio(portfolios, row, surface="profile:user")

    _save_json(cache_path, profile_cache)

    events_by_post = {
        str(row.get("post_id") or ""): row
        for row in trade_events
        if str(row.get("post_id") or "")
    }
    evidence_rows = [
        row["resolver_evidence"]
        for row in events_by_post.values()
        if isinstance(row.get("resolver_evidence"), Mapping)
    ]
    portfolio_metadata = [
        {key: value for key, value in row.items() if key != "surfaces"}
        for row in portfolios.values()
    ]
    with InvoRecordStore(store_path) as store:
        stored_events = store.upsert(
            "events",
            list(events_by_post.values()),
            key_field="post_id",
        )
        stored_evidence = store.upsert(
            "evidence",
            evidence_rows,
            key_field="source_post_id",
        )
        evidence_counts = {
            portfolio_id: len(rows) for portfolio_id, rows in store.evidence_groups()
        }
        resolution = materialize_resolution_queue_from_store(
            store=store,
            output_dir=queue_dir,
            portfolios=portfolio_metadata,
            min_trades=EVIDENCE_THRESHOLD,
        )

    for old in previous.get("candidates", []):
        if not isinstance(old, Mapping):
            continue
        portfolio_id = str(old.get("portfolio_id") or "").strip()
        if not portfolio_id or portfolio_id in portfolios:
            continue
        restored = dict(old)
        restored["surfaces"] = set(old.get("surfaces", []))
        restored["surfaces"].add("previously_seen")
        portfolios[portfolio_id] = restored

    owner_stats = _owner_metrics(social_rows)
    ranked: list[dict[str, object]] = []
    for portfolio_id, row in portfolios.items():
        owner_id = str(row.get("owner_id") or "").strip()
        evidence_count = evidence_counts.get(
            portfolio_id,
            int(row.get("evidence_count") or 0),
        )
        stats = owner_stats.get(
            owner_id,
            {"social_posts": 0, "verified_trade_posts": 0},
        )
        surfaces = row.get("surfaces")
        surface_set = (
            set(surfaces) if isinstance(surfaces, (set, list, tuple)) else set()
        )
        if evidence_count >= EVIDENCE_THRESHOLD:
            stage = "READY_FOR_WALLET_RESOLUTION"
        elif evidence_count > 0 or stats.get("verified_trade_posts", 0) > 0:
            stage = "ACCUMULATING_IDENTITY_EVIDENCE"
        else:
            stage = "DISCOVERED_WATCH"
        ranked.append(
            {
                **{key: value for key, value in row.items() if key != "surfaces"},
                "portfolio_id": portfolio_id,
                "surfaces": sorted(surface_set),
                "leaderboard_timeframes": sorted(
                    surface.removeprefix("leaderboard:")
                    for surface in surface_set
                    if surface.startswith("leaderboard:")
                ),
                "evidence_count": evidence_count,
                "social_posts": int(stats.get("social_posts", 0)),
                "verified_trade_posts": int(stats.get("verified_trade_posts", 0)),
                "tracking_stage": stage,
                "screen_score": _screen_score(
                    row,
                    evidence_count=evidence_count,
                    owner_stats=stats,
                ),
            }
        )
    ranked.sort(key=lambda row: float(row["screen_score"]), reverse=True)

    portfolio_owner_ids = {
        str(row.get("owner_id") or "").strip()
        for row in ranked
        if str(row.get("owner_id") or "").strip()
    }
    discovered_owner_ids = {owner_id for owner_id in owner_priority if owner_id}
    payload = {
        "source": "invo",
        "mode": "EXHAUSTIVE_MULTI_SURFACE_MULTI_PORTFOLIO_V2",
        "real_trading": False,
        "leaderboard_filters": {
            FILTER_LABELS[name]: name for name in PORTFOLIO_FILTERS
        },
        "feed_filters": list(FEED_FILTERS),
        "candidate_portfolio_count": len(ranked),
        "candidate_owner_count": len(portfolio_owner_ids),
        "discovered_owner_count": len(discovered_owner_ids),
        "owners_without_portfolios": sorted(discovered_owner_ids - portfolio_owner_ids),
        "ready_for_wallet_resolution": sum(
            row["tracking_stage"] == "READY_FOR_WALLET_RESOLUTION" for row in ranked
        ),
        "resolution_queue_count": resolution["ready_count"],
        "new_verified_trade_events": stored_events,
        "new_closed_trade_evidence": stored_evidence,
        "surface_errors": errors,
        "candidates": ranked,
    }
    _save_json(universe_path, payload)
    return payload


async def _main() -> int:
    payload = await run_once(_args())
    print(
        json.dumps(
            {
                "candidate_portfolio_count": payload["candidate_portfolio_count"],
                "candidate_owner_count": payload["candidate_owner_count"],
                "ready_for_wallet_resolution": payload[
                    "ready_for_wallet_resolution"
                ],
                "resolution_queue_count": payload["resolution_queue_count"],
                "new_verified_trade_events": payload["new_verified_trade_events"],
                "new_closed_trade_evidence": payload["new_closed_trade_evidence"],
                "surface_error_count": len(payload["surface_errors"]),
                "top_candidates": payload["candidates"][:25],
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
