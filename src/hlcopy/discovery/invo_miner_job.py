from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hlcopy.discovery.invo_source import (
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
    parser.add_argument("--feed-pages", type=int, default=20)
    parser.add_argument("--page-size", type=int, default=50)
    return parser.parse_args()


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen_post_ids": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seen_post_ids": []}
    if not isinstance(payload, dict):
        return {"seen_post_ids": []}
    seen = payload.get("seen_post_ids")
    if not isinstance(seen, list):
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
    by_portfolio: dict[str, object] = {}
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


async def _collect_new_feed_events(
    client: InvoReadOnlyClient,
    *,
    known_post_ids: set[str],
    pages: int,
    page_size: int,
) -> tuple[list[dict[str, object]], list[str]]:
    new_events: list[dict[str, object]] = []
    newly_seen: list[str] = []
    cursor: str | None = None

    for _ in range(max(1, pages)):
        payload = await client.feed(
            filter_name="all",
            last_post_id=cursor,
            item_limit=max(1, page_size),
        )
        items = payload.get("items")
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            break
        page_items = [item for item in items if isinstance(item, Mapping)]
        if not page_items:
            break

        hit_known = False
        for item in page_items:
            post_id = str(item.get("id") or "").strip()
            if not post_id:
                continue
            if post_id in known_post_ids:
                hit_known = True
                continue
            newly_seen.append(post_id)

        events = verified_trade_events({"items": page_items})
        for event in events:
            if event.post_id and event.post_id not in known_post_ids:
                new_events.append(event.to_dict())

        cursor = str(page_items[-1].get("id") or "").strip() or None
        if hit_known or cursor is None:
            break

    return new_events, newly_seen


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

    async with InvoReadOnlyClient(
        access_token=access_token,
        refresh_token=refresh_token,
    ) as client:
        portfolios = await _discover_portfolios(
            client,
            pages=max(1, args.portfolio_pages),
            page_size=max(1, args.page_size),
        )
        events, newly_seen = await _collect_new_feed_events(
            client,
            known_post_ids=known_ids,
            pages=max(1, args.feed_pages),
            page_size=max(1, args.page_size),
        )

    _save_json(
        portfolios_path,
        {
            "source": "invo",
            "portfolio_count": len(portfolios),
            "portfolios": portfolios,
        },
    )
    _append_ndjson(events_path, events)

    ordered_seen = list(dict.fromkeys(newly_seen + list(known_ids)))
    _save_json(
        state_path,
        {
            "seen_post_ids": ordered_seen[:MAX_SEEN_POST_IDS],
            "portfolio_count": len(portfolios),
            "new_verified_trade_events": len(events),
        },
    )
    return {
        "portfolio_count": len(portfolios),
        "new_verified_trade_events": len(events),
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
