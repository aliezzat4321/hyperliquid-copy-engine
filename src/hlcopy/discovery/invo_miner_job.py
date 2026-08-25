from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hlcopy.discovery.invo_evidence import closed_trade_evidence
from hlcopy.discovery.invo_resolution_queue import (
    materialize_resolution_queue_from_store,
)
from hlcopy.discovery.invo_source import (
    InvoApiError,
    InvoPortfolioCandidate,
    InvoReadOnlyClient,
    portfolio_candidates,
    verified_trade_events,
)
from hlcopy.discovery.invo_store import InvoRecordStore, read_legacy_ndjson

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
    parser.add_argument("--resolution-min-trades", type=int, default=12)
    return parser.parse_args()


def _empty_state() -> dict[str, Any]:
    return {
        "seen_post_ids": [],
        "recent_seen_post_ids": [],
        "recent_cursor": None,
        "recent_frontier_ids": [],
        "backfill_cursor": None,
        "backfill_complete": False,
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt Invo miner state: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"non-object Invo miner state: {path}")
    if not isinstance(payload.get("seen_post_ids"), list):
        payload["seen_post_ids"] = []
    if not isinstance(payload.get("recent_seen_post_ids"), list):
        payload["recent_seen_post_ids"] = []
    payload.setdefault("recent_cursor", None)
    if not isinstance(payload.get("recent_frontier_ids"), list):
        payload["recent_frontier_ids"] = []
    payload.setdefault("backfill_cursor", None)
    payload.setdefault("backfill_complete", False)
    return payload


def _bounded_seen_history(*groups: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for group in groups for value in group))[
        :MAX_SEEN_POST_IDS
    ]


def _merge_recent_history(
    *,
    fetched_ids: Sequence[str],
    previous_ids: Sequence[str],
    active_frontier_ids: Sequence[str],
    resumed_catchup: bool,
) -> list[str]:
    if resumed_catchup:
        # A resumed cursor walks toward older pages, so it must not displace the
        # already-captured head of the feed.
        return _bounded_seen_history(
            previous_ids,
            fetched_ids,
            active_frontier_ids,
        )
    return _bounded_seen_history(
        fetched_ids,
        previous_ids,
        active_frontier_ids,
    )


def _migrate_recent_state(
    *,
    recent_history: Sequence[str],
    explicit_frontier: Sequence[str],
    cursor: str | None,
) -> tuple[list[str], list[str], str | None]:
    if recent_history:
        return list(recent_history), list(explicit_frontier), cursor
    # Before separated recent history existed, both the global history and an
    # explicit frontier could have been derived from a backfill-first prefix.
    # Trust neither: reset the cursor and rebuild from a fresh bounded head scan.
    return [], [], None


def _save_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


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
            page_items = _page_items(payload)
            rows = portfolio_candidates({"items": page_items})
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
        raise InvoApiError("Invo feed response lacked a valid items list")
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
    rows: list[dict[str, object]] = []
    for event in verified_trade_events({"items": page_items}):
        if event.post_id not in new_id_set:
            continue
        row = event.to_dict()
        evidence = closed_trade_evidence(event)
        if evidence is not None:
            row["resolver_evidence"] = evidence
        rows.append(row)
    return rows, new_ids


async def _collect_new_feed_events(
    client: InvoReadOnlyClient,
    *,
    known_post_ids: set[str],
    frontier_post_ids: set[str] | None = None,
    start_cursor: str | None = None,
    pages: int,
    page_size: int,
) -> tuple[list[dict[str, object]], list[str], str | None, bool]:
    """Walk recent feed until the known frontier, persisting a cursor if capped."""
    new_events: list[dict[str, object]] = []
    newly_seen: list[str] = []
    seen_ids = set(known_post_ids)
    cursor = start_cursor
    frontier_ids = set(known_post_ids if frontier_post_ids is None else frontier_post_ids)
    frontier_exists = bool(frontier_ids) or start_cursor is not None
    complete = False

    for _ in range(max(1, pages)):
        payload = await client.feed(
            filter_name="all",
            last_post_id=cursor,
            item_limit=max(1, page_size),
        )
        page_items = _page_items(payload)
        if not page_items:
            cursor = None
            complete = True
            break

        page_had_frontier = any(
            str(item.get("id") or "").strip() in frontier_ids for item in page_items
        )
        events, new_ids = _new_events_from_page(page_items, seen_ids=seen_ids)
        new_events.extend(events)
        newly_seen.extend(new_ids)

        next_cursor = str(page_items[-1].get("id") or "").strip() or None
        if page_had_frontier or next_cursor is None or next_cursor == cursor:
            cursor = None
            complete = True
            break
        cursor = next_cursor
    else:
        if not frontier_exists:
            # First bootstrap has no known recent frontier. Historical backfill owns older
            # history, so do not trap the recent crawler walking the entire feed.
            cursor = None
            complete = True

    return new_events, newly_seen, cursor, complete


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


def _resolver_evidence_rows(
    events: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    for event in events:
        evidence = event.get("resolver_evidence")
        if isinstance(evidence, Mapping):
            rows.append(evidence)
    return rows


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
    evidence_path = state_dir / "closed_trade_evidence.ndjson"
    store_path = state_dir / "archive.sqlite3"
    queue_dir = state_dir / "resolution_queue"

    state = _load_state(state_path)
    seen_values = [
        str(value).strip()
        for value in state.get("seen_post_ids", [])
        if str(value).strip()
    ]
    known_ids = set(seen_values)
    recent_cursor_raw = state.get("recent_cursor")
    recent_cursor = str(recent_cursor_raw) if recent_cursor_raw else None
    frontier_values = [
        str(value).strip()
        for value in state.get("recent_frontier_ids", [])
        if str(value).strip()
    ]
    recent_history_values = [
        str(value).strip()
        for value in state.get("recent_seen_post_ids", [])
        if str(value).strip()
    ]
    recent_history_values, frontier_values, recent_cursor = _migrate_recent_state(
        recent_history=recent_history_values,
        explicit_frontier=frontier_values,
        cursor=recent_cursor,
    )
    resumed_recent_catchup = recent_cursor is not None
    if recent_cursor and not frontier_values:
        frontier_values = recent_history_values[: max(100, args.page_size * 2)]
    if not recent_cursor:
        frontier_values = recent_history_values[: max(100, args.page_size * 2)]
    recent_frontier_ids = set(frontier_values)
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
        (
            recent_events,
            recent_seen,
            recent_cursor,
            recent_complete,
        ) = await _collect_new_feed_events(
            client,
            known_post_ids=known_ids,
            frontier_post_ids=recent_frontier_ids,
            start_cursor=recent_cursor,
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
    evidence_rows = _resolver_evidence_rows(events)

    _save_json(
        portfolios_path,
        {
            "source": "invo",
            "portfolio_count": len(portfolios),
            "portfolios": portfolios,
        },
    )
    with InvoRecordStore(store_path) as store:
        if not bool(state.get("legacy_archive_imported", False)):
            store.upsert(
                "events",
                read_legacy_ndjson(events_path),
                key_field="post_id",
            )
            store.upsert(
                "evidence",
                read_legacy_ndjson(evidence_path),
                key_field="source_post_id",
            )
        stored_events = store.upsert("events", events, key_field="post_id")
        stored_evidence = store.upsert(
            "evidence",
            evidence_rows,
            key_field="source_post_id",
        )
        resolution = materialize_resolution_queue_from_store(
            store=store,
            output_dir=queue_dir,
            portfolios=portfolios,
            min_trades=max(3, args.resolution_min_trades),
        )

    active_frontier = [] if recent_complete else frontier_values
    ordered_recent_seen = _merge_recent_history(
        fetched_ids=recent_seen,
        previous_ids=recent_history_values,
        active_frontier_ids=active_frontier,
        resumed_catchup=resumed_recent_catchup,
    )
    ordered_seen = _bounded_seen_history(
        ordered_recent_seen,
        backfill_seen,
        seen_values,
    )
    _save_json(
        state_path,
        {
            "seen_post_ids": ordered_seen,
            "recent_seen_post_ids": ordered_recent_seen,
            "portfolio_count": len(portfolios),
            "new_verified_trade_events": stored_events,
            "new_closed_trade_evidence": stored_evidence,
            "resolution_ready_count": resolution["ready_count"],
            "recent_cursor": recent_cursor,
            "recent_frontier_ids": active_frontier,
            "recent_catchup_active": not recent_complete,
            "backfill_cursor": backfill_cursor,
            "backfill_complete": backfill_complete,
            "legacy_archive_imported": True,
            "archive_store": str(store_path),
        },
    )
    return {
        "portfolio_count": len(portfolios),
        "new_verified_trade_events": stored_events,
        "new_closed_trade_evidence": stored_evidence,
        "resolution_ready_count": resolution["ready_count"],
        "recent_catchup_active": not recent_complete,
        "recent_cursor": recent_cursor,
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
