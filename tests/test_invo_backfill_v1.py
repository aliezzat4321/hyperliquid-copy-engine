from __future__ import annotations

import asyncio

from hlcopy.discovery.invo_miner_job import _collect_backfill_feed_events


class _BackfillClient:
    async def feed(
        self,
        *,
        filter_name: str,
        last_post_id: str | None,
        item_limit: int,
    ) -> dict[str, object]:
        assert filter_name == "all"
        assert item_limit == 50
        if last_post_id is None:
            return {
                "items": [
                    {
                        "id": "newest-3",
                        "update": {
                            "ticker": "HYPE",
                            "directionLong": True,
                            "verifiedTrade": True,
                        },
                    },
                    {
                        "id": "newest-2",
                        "update": {
                            "ticker": "SOL",
                            "directionLong": False,
                            "verifiedTrade": True,
                        },
                    },
                ]
            }
        if last_post_id == "newest-2":
            return {
                "items": [
                    {
                        "id": "old-1",
                        "update": {
                            "ticker": "ETH",
                            "directionLong": True,
                            "verifiedTrade": True,
                        },
                    },
                    {
                        "id": "old-0",
                        "update": {
                            "ticker": "BTC",
                            "directionLong": False,
                            "verifiedTrade": True,
                        },
                    },
                ]
            }
        return {"items": []}


def test_backfill_cursor_continues_across_runs_until_history_is_exhausted() -> None:
    client = _BackfillClient()
    first_events, first_seen, cursor, complete = asyncio.run(
        _collect_backfill_feed_events(
            client,  # type: ignore[arg-type]
            known_post_ids=set(),
            start_cursor=None,
            pages=1,
            page_size=50,
        )
    )

    assert {event["post_id"] for event in first_events} == {"newest-3", "newest-2"}
    assert first_seen == ["newest-3", "newest-2"]
    assert cursor == "newest-2"
    assert not complete

    second_events, second_seen, cursor, complete = asyncio.run(
        _collect_backfill_feed_events(
            client,  # type: ignore[arg-type]
            known_post_ids=set(first_seen),
            start_cursor=cursor,
            pages=2,
            page_size=50,
        )
    )

    assert {event["post_id"] for event in second_events} == {"old-1", "old-0"}
    assert second_seen == ["old-1", "old-0"]
    assert cursor == "old-0"
    assert complete
