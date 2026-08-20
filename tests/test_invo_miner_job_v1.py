from __future__ import annotations

import asyncio

from hlcopy.discovery.invo_miner_job import _collect_new_feed_events


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def feed(
        self,
        *,
        filter_name: str,
        last_post_id: str | None,
        item_limit: int,
    ) -> dict[str, object]:
        assert filter_name == "all"
        assert item_limit == 50
        self.calls.append(last_post_id)
        if last_post_id is None:
            return {
                "items": [
                    {
                        "id": "new-2",
                        "update": {
                            "ticker": "HYPE",
                            "directionLong": True,
                            "verifiedTrade": True,
                        },
                    },
                    {
                        "id": "new-1",
                        "update": {
                            "ticker": "SOL",
                            "directionLong": False,
                            "verifiedTrade": True,
                        },
                    },
                ]
            }
        return {
            "items": [
                {
                    "id": "known",
                    "update": {
                        "ticker": "BTC",
                        "directionLong": True,
                        "verifiedTrade": True,
                    },
                }
            ]
        }


def test_collector_pages_until_known_post_then_stops() -> None:
    client = _FakeClient()
    events, seen = asyncio.run(
        _collect_new_feed_events(
            client,  # type: ignore[arg-type]
            known_post_ids={"known"},
            pages=10,
            page_size=50,
        )
    )

    assert client.calls == [None, "new-1"]
    assert {event["post_id"] for event in events} == {"new-1", "new-2"}
    assert seen == ["new-2", "new-1"]
