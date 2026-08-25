from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hlcopy.discovery.invo_miner_job import (
    _bounded_seen_history,
    _collect_new_feed_events,
    _load_state,
    _page_items,
)
from hlcopy.discovery.invo_source import InvoApiError
from hlcopy.discovery.invo_store import InvoRecordStore


def _post(post_id: str) -> dict[str, object]:
    return {
        "id": post_id,
        "update": {
            "ticker": "HYPE",
            "directionLong": True,
            "verifiedTrade": True,
        },
    }


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
            return {"items": [_post("new-2"), _post("new-1")]}
        return {"items": [_post("known")]}


def test_collector_pages_until_known_post_then_stops() -> None:
    client = _FakeClient()
    events, seen, cursor, complete = asyncio.run(
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
    assert cursor is None
    assert complete is True


class _CatchupClient:
    def __init__(self) -> None:
        self.calls: list[str | None] = []
        self.pages: dict[str | None, list[str]] = {
            None: ["new-6", "new-5"],
            "new-5": ["new-4", "new-3"],
            "new-3": ["new-2", "new-1"],
            "new-1": ["known"],
        }

    async def feed(
        self,
        *,
        filter_name: str,
        last_post_id: str | None,
        item_limit: int,
    ) -> dict[str, object]:
        assert filter_name == "all"
        assert item_limit == 2
        self.calls.append(last_post_id)
        return {"items": [_post(post_id) for post_id in self.pages.get(last_post_id, [])]}


def test_recent_catchup_cursor_recovers_gap_across_runs_without_duplicates() -> None:
    client = _CatchupClient()
    first_events, first_seen, cursor, complete = asyncio.run(
        _collect_new_feed_events(
            client,  # type: ignore[arg-type]
            known_post_ids={"known"},
            pages=2,
            page_size=2,
        )
    )

    assert [event["post_id"] for event in first_events] == [
        "new-6",
        "new-5",
        "new-4",
        "new-3",
    ]
    assert first_seen == ["new-6", "new-5", "new-4", "new-3"]
    assert cursor == "new-3"
    assert complete is False

    second_events, second_seen, cursor, complete = asyncio.run(
        _collect_new_feed_events(
            client,  # type: ignore[arg-type]
            known_post_ids={"known", *first_seen},
            start_cursor=cursor,
            pages=2,
            page_size=2,
        )
    )

    assert [event["post_id"] for event in second_events] == ["new-2", "new-1"]
    assert second_seen == ["new-2", "new-1"]
    assert cursor is None
    assert complete is True
    all_seen = first_seen + second_seen
    assert set(all_seen) == {f"new-{index}" for index in range(1, 7)}
    assert len(all_seen) == len(set(all_seen)) == 6
    assert client.calls == [None, "new-5", "new-3", "new-1"]


class _FrontierClient:
    async def feed(
        self,
        *,
        filter_name: str,
        last_post_id: str | None,
        item_limit: int,
    ) -> dict[str, object]:
        del filter_name, item_limit
        if last_post_id == "resume":
            return {"items": [_post("seen-during-catchup"), _post("new-1")]}
        if last_post_id == "new-1":
            return {"items": [_post("original-frontier")]}
        return {"items": []}


def test_active_catchup_stops_only_at_persisted_frontier() -> None:
    events, seen, cursor, complete = asyncio.run(
        _collect_new_feed_events(
            _FrontierClient(),  # type: ignore[arg-type]
            known_post_ids={"seen-during-catchup", "original-frontier"},
            frontier_post_ids={"original-frontier"},
            start_cursor="resume",
            pages=3,
            page_size=2,
        )
    )

    assert [event["post_id"] for event in events] == ["new-1"]
    assert seen == ["new-1"]
    assert cursor is None
    assert complete


def test_recent_ids_stay_ahead_of_large_backfill_history() -> None:
    recent = ["recent-new", "recent-frontier"]
    backfill = [f"old-{index}" for index in range(25_000)]

    merged = _bounded_seen_history(recent, backfill)

    assert merged[:2] == recent
    assert len(merged) == 20_000
    assert "recent-frontier" in merged


def test_malformed_page_fails_closed() -> None:
    with pytest.raises(InvoApiError, match="items list"):
        _page_items({"unexpected": []})


def test_indexed_store_is_durable_and_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "archive.sqlite3"
    with InvoRecordStore(path) as store:
        assert store.upsert(
            "evidence",
            [{"source_post_id": "p1", "portfolio_id": "one", "value": 1}],
            key_field="source_post_id",
        ) == 1
        assert store.upsert(
            "evidence",
            [
                {"source_post_id": "p1", "portfolio_id": "one", "value": 2},
                {"source_post_id": "p2", "portfolio_id": "one", "value": 3},
            ],
            key_field="source_post_id",
        ) == 1

    with InvoRecordStore(path) as store:
        groups = list(store.evidence_groups())
    assert groups == [
        (
            "one",
            [
                {"source_post_id": "p1", "portfolio_id": "one", "value": 2},
                {"source_post_id": "p2", "portfolio_id": "one", "value": 3},
            ],
        )
    ]


def test_corrupt_durable_state_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("{truncated", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt Invo miner state"):
        _load_state(state)
