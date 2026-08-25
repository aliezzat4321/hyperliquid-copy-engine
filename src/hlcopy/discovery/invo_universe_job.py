from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hlcopy.discovery.invo_evidence import closed_trade_evidence
from hlcopy.discovery.invo_miner_job import _discover_portfolios, _page_items
from hlcopy.discovery.invo_resolution_queue import materialize_resolution_queue_from_store
from hlcopy.discovery.invo_source import (
    InvoApiError,
    InvoReadOnlyClient,
    verified_trade_events,
)
from hlcopy.discovery.invo_store import InvoRecordStore

DEFAULT_STATE_DIR = Path("/var/lib/hyperliquid-copy-engine/invo")
FEED_FILTERS = ("trending", "following", "all")
EVIDENCE_THRESHOLD = 20


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exhaustive read-only Invo universe miner")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--portfolio-pages", type=int, default=10)
    parser.add_argument("--feed-pages", type=int, default=6)
    parser.add_argument("--page-size", type=int, default=50)
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
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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


def _evidence(events: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    for event in events:
        raw = event.get("raw")
        if not isinstance(raw, Mapping):
            continue
    return rows


def _store_counts(store_path: Path) -> tuple[dict[str, int], dict[str, tuple[str, str]]]:
    evidence_by_portfolio: dict[str, int] = {}
    owner_portfolios: dict[str, tuple[str, str]] = {}
    if not store_path.exists():
        return evidence_by_portfolio, owner_portfolios
    connection = sqlite3.connect(store_path)
    try:
        for portfolio_id, count in connection.execute(
            "SELECT portfolio_id, COUNT(*) FROM records "
            "WHERE stream='evidence' AND portfolio_id<>'' GROUP BY portfolio_id"
        ):
            evidence_by_portfolio[str(portfolio_id)] = int(count)
        for (payload,) in connection.execute(
            "SELECT payload FROM records WHERE stream='events'"
        ):
            try:
                row = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            owner_id = str(row.get("owner_id") or "").strip()
            portfolio_id = str(row.get("portfolio_id") or "").strip()
            username = str(row.get("username") or "").strip()
            if owner_id and portfolio_id:
                owner_portfolios[owner_id] = (portfolio_id, username)
    finally:
        connection.close()
    return evidence_by_portfolio, owner_portfolios


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
            social_row = _social_row(post, filter_name=filter_name)
            if social_row is not None:
                social.append(social_row)
        for event in verified_trade_events({"items": page}):
            if event.post_id not in seen:
                continue
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


async def run_once(args: argparse.Namespace) -> dict[str, object]:
    access_token = os.getenv("INVO_ACCESS_TOKEN")
    refresh_token = os.getenv("INVO_REFRESH_TOKEN")
    if not access_token and not refresh_token:
        raise RuntimeError("Invo authentication is missing")

    state_dir: Path = args.state_dir
    store_path = state_dir / "archive.sqlite3"
    queue_dir = state_dir / "resolution_queue"
    previous_path = state_dir / "universe_candidates.json"
    errors: list[str] = []
    social_rows: list[dict[str, object]] = []
    trade_events: list[dict[str, object]] = []
    trending_users: list[Mapping[str, Any]] = []

    async with InvoReadOnlyClient(
        access_token=access_token,
        refresh_token=refresh_token,
        timeout_seconds=10.0,
        retry_attempts=2,
    ) as client:
        try:
            portfolios = await _discover_portfolios(
                client,
                pages=max(1, args.portfolio_pages),
                page_size=max(1, args.page_size),
            )
        except InvoApiError as exc:
            errors.append(f"portfolios:{exc}")
            previous = _load_json(state_dir / "latest_portfolios.json", {})
            raw = previous.get("portfolios", [])
            portfolios = [dict(row) for row in raw if isinstance(row, Mapping)]

        for page_number in range(1, 5):
            try:
                payload = await client.trending_users(page=page_number, size=50)
            except InvoApiError as exc:
                errors.append(f"trending_users:{page_number}:{exc}")
                break
            page = _page_items(payload)
            if not page:
                break
            trending_users.extend(page)

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

    with InvoRecordStore(store_path) as store:
        stored_events = store.upsert("events", list(events_by_post.values()), key_field="post_id")
        stored_evidence = store.upsert(
            "evidence", evidence_rows, key_field="source_post_id"
        )
        resolution = materialize_resolution_queue_from_store(
            store=store,
            output_dir=queue_dir,
            portfolios=portfolios,
            min_trades=EVIDENCE_THRESHOLD,
        )

    evidence_by_portfolio, owner_portfolios = _store_counts(store_path)
    candidate: dict[str, dict[str, object]] = {}
    for row in portfolios:
        owner_id = str(row.get("owner_id") or "").strip()
        if not owner_id:
            continue
        candidate[owner_id] = {
            "owner_id": owner_id,
            "username": str(row.get("username") or ""),
            "portfolio_id": str(row.get("portfolio_id") or ""),
            "closed_positions": int(row.get("closed_positions") or 0),
            "win_rate": float(row.get("win_rate") or 0.0),
            "percent_change": float(row.get("percent_change") or 0.0),
            "followers": int(row.get("follower_count") or 0),
            "liquidated": bool(row.get("liquidated", False)),
            "surfaces": {"portfolio"},
            "social_posts": 0,
            "verified_trade_posts": 0,
        }

    for row in trending_users:
        owner_id = str(row.get("id") or row.get("userId") or "").strip()
        if not owner_id:
            continue
        rec = candidate.setdefault(
            owner_id,
            {
                "owner_id": owner_id,
                "username": str(row.get("username") or ""),
                "portfolio_id": "",
                "closed_positions": 0,
                "win_rate": 0.0,
                "percent_change": 0.0,
                "followers": 0,
                "liquidated": False,
                "surfaces": set(),
                "social_posts": 0,
                "verified_trade_posts": 0,
            },
        )
        rec["surfaces"].add("trending_users")  # type: ignore[union-attr]

    for row in social_rows:
        owner_id = str(row.get("owner_id") or "").strip()
        if not owner_id:
            continue
        rec = candidate.setdefault(
            owner_id,
            {
                "owner_id": owner_id,
                "username": str(row.get("username") or ""),
                "portfolio_id": str(row.get("portfolio_id") or ""),
                "closed_positions": 0,
                "win_rate": 0.0,
                "percent_change": 0.0,
                "followers": 0,
                "liquidated": False,
                "surfaces": set(),
                "social_posts": 0,
                "verified_trade_posts": 0,
            },
        )
        if not rec.get("username") and row.get("username"):
            rec["username"] = row["username"]
        if not rec.get("portfolio_id") and row.get("portfolio_id"):
            rec["portfolio_id"] = row["portfolio_id"]
        rec["surfaces"].add(f"feed:{row.get('feed_filter')}")  # type: ignore[union-attr]
        rec["social_posts"] = int(rec["social_posts"]) + 1
        rec["verified_trade_posts"] = int(rec["verified_trade_posts"]) + int(
            bool(row.get("verified_trade"))
        )

    previous = _load_json(previous_path, {})
    for old in previous.get("candidates", []):
        if not isinstance(old, Mapping):
            continue
        owner_id = str(old.get("owner_id") or "").strip()
        if owner_id and owner_id not in candidate:
            restored = dict(old)
            restored["surfaces"] = set(old.get("surfaces", []))
            candidate[owner_id] = restored

    ranked: list[dict[str, object]] = []
    for owner_id, rec in candidate.items():
        portfolio_id = str(rec.get("portfolio_id") or "").strip()
        if not portfolio_id and owner_id in owner_portfolios:
            portfolio_id, event_username = owner_portfolios[owner_id]
            rec["portfolio_id"] = portfolio_id
            if not rec.get("username"):
                rec["username"] = event_username
        evidence_count = evidence_by_portfolio.get(portfolio_id, 0)
        closed = int(rec.get("closed_positions") or 0)
        win_rate = float(rec.get("win_rate") or 0.0)
        percent_change = float(rec.get("percent_change") or 0.0)
        followers = int(rec.get("followers") or 0)
        social_posts = int(rec.get("social_posts") or 0)
        verified_posts = int(rec.get("verified_trade_posts") or 0)
        surfaces = set(rec.get("surfaces", set()))
        score = (
            min(closed, 500) * 0.2
            + min(max(win_rate, 0.0), 100.0)
            + min(max(percent_change, 0.0), 5000.0) * 0.02
            + min(followers, 10000) * 0.002
            + min(social_posts, 100) * 0.5
            + min(verified_posts, 100) * 1.5
            + min(evidence_count, 100) * 2.0
            + len(surfaces) * 10
            - (100 if bool(rec.get("liquidated")) else 0)
        )
        if evidence_count >= EVIDENCE_THRESHOLD:
            stage = "READY_FOR_WALLET_RESOLUTION"
        elif evidence_count > 0 or verified_posts > 0:
            stage = "ACCUMULATING_IDENTITY_EVIDENCE"
        else:
            stage = "SOCIAL_AND_LEADERBOARD_WATCH"
        ranked.append(
            {
                **{k: v for k, v in rec.items() if k != "surfaces"},
                "surfaces": sorted(surfaces),
                "evidence_count": evidence_count,
                "tracking_stage": stage,
                "screen_score": round(score, 3),
            }
        )
    ranked.sort(key=lambda row: float(row["screen_score"]), reverse=True)

    payload = {
        "source": "invo",
        "candidate_count": len(ranked),
        "ready_for_wallet_resolution": sum(
            row["tracking_stage"] == "READY_FOR_WALLET_RESOLUTION" for row in ranked
        ),
        "resolution_queue_count": resolution["ready_count"],
        "new_verified_trade_events": stored_events,
        "new_closed_trade_evidence": stored_evidence,
        "surface_errors": errors,
        "candidates": ranked,
    }
    _save_json(previous_path, payload)
    return payload


async def _main() -> int:
    payload = await run_once(_args())
    print(
        json.dumps(
            {
                "candidate_count": payload["candidate_count"],
                "ready_for_wallet_resolution": payload["ready_for_wallet_resolution"],
                "resolution_queue_count": payload["resolution_queue_count"],
                "new_verified_trade_events": payload["new_verified_trade_events"],
                "new_closed_trade_evidence": payload["new_closed_trade_evidence"],
                "surface_errors": payload["surface_errors"],
                "top_candidates": payload["candidates"][:20],
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
