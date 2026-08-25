from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from hlcopy.discovery.invo_source import InvoReadOnlyClient, portfolio_candidates


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Invo public-profile/portfolio discovery for research.",
    )
    parser.add_argument("--username", help="Optional Invo username, without @")
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    access_token = os.getenv("INVO_ACCESS_TOKEN")
    refresh_token = os.getenv("INVO_REFRESH_TOKEN")
    if not access_token and not refresh_token:
        raise SystemExit(
            "Set INVO_ACCESS_TOKEN or INVO_REFRESH_TOKEN in the runtime environment; "
            "do not put Invo credentials in source control."
        )

    by_portfolio: dict[str, object] = {}
    async with InvoReadOnlyClient(
        access_token=access_token,
        refresh_token=refresh_token,
    ) as client:
        for filter_name in ("trending", "all"):
            for page in range(1, max(1, args.pages) + 1):
                payload = await client.discover_portfolios(
                    filter_name=filter_name,
                    page=page,
                    size=max(1, args.page_size),
                )
                rows = portfolio_candidates(payload)
                if not rows:
                    break
                for row in rows:
                    by_portfolio[row.portfolio_id] = row

        rows = list(by_portfolio.values())
        if args.username:
            target = args.username.removeprefix("@").casefold()
            rows = [row for row in rows if row.username.casefold() == target]

    rows.sort(
        key=lambda row: (
            row.closed_positions,
            row.percent_change,
            row.win_rate,
            row.follower_count,
        ),
        reverse=True,
    )
    payload = {
        "source": "invo",
        "mode": "read_only_profile_discovery",
        "username_filter": args.username,
        "portfolio_count": len(rows),
        "portfolios": [row.to_dict() for row in rows],
        "warning": (
            "Headline win rate/percent change are discovery metadata only and must not "
            "be used as a live-trading promotion gate."
        ),
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
