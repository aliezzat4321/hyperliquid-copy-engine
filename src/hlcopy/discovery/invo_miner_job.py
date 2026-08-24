from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hlcopy.discovery.invo_source import (
    InvoPortfolioCandidate,
    InvoReadOnlyClient,
    portfolio_candidates,
    verified_trade_events,
)

DEFAULT_STATE_DIR = Path("/var/lib/hyperliquid-copy-engine/invo")
MAX_SEEN_POST_IDS = 20_000


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Unattended read-only Invo trader/feed source miner.",
    )
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--portfolio-pages", type=int, default=5)
    parser.add_argument("--recent-feed-pages", type=int, default=3)
    parser.add_argument("--backfill-pages", type=int, default=10)
    parser.add_argument("--page-size", type=int, default=50)
    return parser.parse_args()


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen_post_ids": [], "backfill_cursor": None, "backfill_complete": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seen_post_ids": [], "backfill_cursor": None, "backfill_complete": False}
    if not isinstance(payload, dict):
        return {"seen_post_ids": [], "backfill_cursor": None, "backfill_complete": False}
    if not isinstance(payload.get("seen_post_ids"), list):
        payload["seen_post_ids"] = []
    return payload


def _save_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_ndjson(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), sort_keys=True) + "\n")


async def _discover_portfolios(
    client: InvoReadOnlyClient,
    *,
    pages: int,
    page_size: int,
) -> list[dict[str, object]]:
    by_portfolio: dict[str, InvoPortfolioCandidate] = {}
    for filter_name in ("trending", "all"):
        for page in range(1, max(1, pages) + 1):
            payload = await client.discover_portfolios(
                filter_name=filter_name,
                page=page,
                size=max(1, page_size),
            )
            rows = portfolio_candidates(payload)
            if not rows:
                break
            for row in rows:
                by_portfolio[row.portfolio_id] = row

    rows = list(by_portfolio.values())
    rows.sort(
        key=lambda row: (
            row.closed_positions,
            row.percent_change,
            row.win_rate,
            row.follower_count,
        ),
        reverse=True,
    )
    return [row.to_dict() for row in rows]


def _page_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = payload.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return []
    return [item for item in items if isinstance(item, Mapping)]


def _new_events_from_page(
    page_items: Sequence[Mapping[str, Any]],
    *,
    seen_ids: set[str],
) -> tuple[list[dict[str, object]], list[str]]:
    new_ids: list[str] = []
    for item in page_items:
        post_id = str(item.get("id") or "").strip()
        if not post_id or post_id in seen_ids:
            continue
        seen_ids.add(post_id)
        new_ids.append(post_id)

    new_id_set = set(new_ids)
    events = [
        event.to_dict()
        for event in verified_trade_events({"items": page_items})
        if event.post_id in new_id_set
    ]
    return events, new_ids


async def _collect_new_feed_events(
    client: InvoReadOnlyClient,
    *,
    known_post_ids: set[str],
    pages: int,
    page_size: int,
) -> tuple[list[dict[str, object]], list[str]]:
    new_events: list[dict[str, object]] = []
    newly_seen: list[str] = []
    seen_ids = set(known_post_ids)
    cursor: str | None = None

    for _ in range(max(1, pages)):
        payload = await client.feed(
            filter_name="all",
            last_post_id=cursor,
            item_limit=max(1, page_size),
        )
        page_items = _page_items(payload)
        if not page_items:
            break

        page_had_known = any(
            str(item.get("id") or "").strip() in known_post_ids for item in page_items
        )
        events, new_ids = _new_events_from_page(page_items, seen_ids=seen_ids)
        new_events.extend(events)
        newly_seen.extend(new_ids)

        next_cursor = str(page_items[-1].get("id") or "").strip() or None
        if page_had_known or next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor

    return new_events, newly_seen


async def _collect_backfill_feed_events(
    client: InvoReadOnlyClient,
    *,
    known_post_ids: set[str],
    start_cursor: str | None,
    pages: int,
    page_size: int,
) -> tuple[list[dict[str, object]], list[str], str | None, bool]:
    new_events: list[dict[str, object]] = []
    newly_seen: list[str] = []
    seen_ids = set(known_post_ids)
    cursor = start_cursor
    complete = False

    for _ in range(max(1, pages)):
        payload = await client.feed(
            filter_name="all",
            last_post_id=cursor,
            item_limit=max(1, page_size),
        )
        page_items = _page_items(payload)
        if not page_items:
            complete = True
            break

        events, new_ids = _new_events_from_page(page_items, seen_ids=seen_ids)
        new_events.extend(events)
        newly_seen.extend(new_ids)

        next_cursor = str(page_items[-1].get("id") or "").strip() or None
        if next_cursor is None or next_cursor == cursor:
            complete = True
            break
        cursor = next_cursor

    return new_events, newly_seen, cursor, complete


async def run_once(args: argparse.Namespace) -> dict[str, object]:
    access_token = os.getenv("INVO_ACCESS_TOKEN")
    refresh_token = os.getenv("INVO_REFRESH_TOKEN")
    if not access_token and not refresh_token:
        raise RuntimeError(
            "Invo auth missing: set INVO_REFRESH_TOKEN (preferred) or INVO_ACCESS_TOKEN"
        )

    state_dir: Path = args.state_dir
    state_path = state_dir / "state.json"
    portfolios_path = state_dir / "latest_portfolios.json"
    events_path = state_dir / "verified_trade_events.ndjson"

    state = _load_state(state_path)
    known_ids = {
        str(value)
        for value in state.get("seen_post_ids", [])
        if str(value).strip()
    }
    backfill_cursor_raw = state.get("backfill_cursor")
    backfill_cursor = str(backfill_cursor_raw) if backfill_cursor_raw else None
    backfill_complete = bool(state.get("backfill_complete", False))

    async with InvoReadOnlyClient(
        access_token=access_token,
        refresh_token=refresh_token,
    ) as client:
        portfolios = await _discover_portfolios(
            client,
            pages=max(1, args.portfolio_pages),
            page_size=max(1, args.page_size),
        )
        recent_events, recent_seen = await _collect_new_feed_events(
            client,
            known_post_ids=known_ids,
            pages=max(1, args.recent_feed_pages),
            page_size=max(1, args.page_size),
        )

        all_known_ids = known_ids | set(recent_seen)
        backfill_events: list[dict[str, object]] = []
        backfill_seen: list[str] = []
        if not backfill_complete:
            (
                backfill_events,
                backfill_seen,
                backfill_cursor,
                backfill_complete,
            ) = await _collect_backfill_feed_events(
                client,
                known_post_ids=all_known_ids,
                start_cursor=backfill_cursor,
                pages=max(1, args.backfill_pages),
                page_size=max(1, args.page_size),
            )

    events_by_post = {
        str(event["post_id"]): event for event in recent_events + backfill_events
    }
    events = list(events_by_post.values())

    _save_json(
        portfolios_path,
        {
            "source": "invo",
            "portfolio_count": len(portfolios),
            "portfolios": portfolios,
        },
    )
    _append_ndjson(events_path, events)

    ordered_seen = list(
        dict.fromkeys(recent_seen + backfill_seen + sorted(known_ids))
    )
    _save_json(
        state_path,
        {
            "seen_post_ids": ordered_seen[:MAX_SEEN_POST_IDS],
            "portfolio_count": len(portfolios),
            "new_verified_trade_events": len(events),
            "backfill_cursor": backfill_cursor,
            "backfill_complete": backfill_complete,
        },
    )
    return {
        "portfolio_count": len(portfolios),
        "new_verified_trade_events": len(events),
        "backfill_complete": backfill_complete,
        "backfill_cursor": backfill_cursor,
        "state_dir": str(state_dir),
    }


async def _main() -> int:
    result = await run_once(_parse_args())
    print(json.dumps(result, sort_keys=True))
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
