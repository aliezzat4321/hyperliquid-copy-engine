from __future__ import annotations

import asyncio
import json
import os

from hlcopy.discovery.invo_source import InvoApiError, InvoReadOnlyClient

FILTERS = (
    "1D", "1W", "1M", "1Y", "AT",
    "1d", "1w", "1m", "1y", "at",
    "day", "week", "month", "year", "all_time", "allTime",
    "daily", "weekly", "monthly", "yearly", "all", "trending",
)


def _username(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    return str(owner.get("username") or item.get("username") or "")


async def main() -> int:
    refresh = os.getenv("INVO_REFRESH_TOKEN")
    access = os.getenv("INVO_ACCESS_TOKEN")
    if not refresh and not access:
        raise RuntimeError("missing Invo auth")
    async with InvoReadOnlyClient(
        refresh_token=refresh,
        access_token=access,
        timeout_seconds=8.0,
        retry_attempts=1,
    ) as client:
        for filter_name in FILTERS:
            try:
                payload = await client.discover_portfolios(
                    filter_name=filter_name,
                    page=1,
                    size=10,
                )
            except InvoApiError as exc:
                print(json.dumps({"filter": filter_name, "ok": False, "error": str(exc)}))
                continue
            items = payload.get("items") if isinstance(payload, dict) else None
            rows = items if isinstance(items, list) else []
            print(json.dumps({
                "filter": filter_name,
                "ok": True,
                "count": len(rows),
                "users": [_username(row) for row in rows[:10]],
            }))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
