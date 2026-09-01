from __future__ import annotations

import asyncio

from hlcopy.db.postgres import Database
from hlcopy.discovery.leaderboard import LeaderboardCandidate, WindowPerformance


class _FakeCursor:
    def __init__(self, row: tuple[object, ...] | None = (None,)) -> None:
        self.row = row

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.row


class _FakeTransaction:
    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeConn:
    def __init__(self, latest_snapshot: object = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.latest_snapshot = latest_snapshot

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, query: str, params: object = None) -> _FakeCursor:
        self.calls.append((query, params))
        if "SELECT max(snapshot_at)" in query:
            return _FakeCursor((self.latest_snapshot,))
        return _FakeCursor()


def test_store_raw_keeps_observation_but_payload_is_content_addressed_once() -> None:
    db = Database("unused")
    conn = _FakeConn()
    db.conn = conn  # type: ignore[assignment]

    asyncio.run(
        db.store_raw(
            source="hyperliquid",
            endpoint="userFills",
            request_payload={"type": "userFills", "user": "0xabc"},
            response_payload=[{"tid": 1}],
            fetched_at_ms=1_000,
        )
    )

    assert len(conn.calls) == 2
    payload_query, payload_params = conn.calls[0]
    observation_query, observation_params = conn.calls[1]
    assert "INSERT INTO raw_api_payloads" in payload_query
    assert "ON CONFLICT(content_sha256) DO NOTHING" in payload_query
    assert "INSERT INTO raw_api_responses" in observation_query
    assert "'{}'::jsonb" in observation_query
    assert "0xabc" in repr(observation_params)
    assert "tid" in repr(payload_params)


def test_leaderboard_rows_do_not_repeat_large_raw_candidate_json() -> None:
    db = Database("unused")
    conn = _FakeConn()
    db.conn = conn  # type: ignore[assignment]
    candidate = LeaderboardCandidate(
        address="0x" + "a" * 40,
        display_name="test",
        account_value=100_000.0,
        windows={"month": WindowPerformance(pnl=1_000, roi=0.1, volume=1_000_000)},
        raw={"large_marker_that_must_not_be_repeated": "x" * 1_000},
    )

    asyncio.run(db.upsert_leaderboard([candidate], 1_000))

    snapshot_calls = [
        (query, params)
        for query, params in conn.calls
        if "INSERT INTO leaderboard_snapshots" in query
    ]
    assert len(snapshot_calls) == 1
    assert "large_marker_that_must_not_be_repeated" not in repr(snapshot_calls[0][1])
    assert "'x'" not in repr(snapshot_calls[0][1])
    assert "'{}'::jsonb" in snapshot_calls[0][0]


def test_leaderboard_history_skips_snapshot_inside_four_hour_window() -> None:
    from datetime import UTC, datetime, timedelta

    latest = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    db = Database("unused")
    conn = _FakeConn(latest_snapshot=latest)
    db.conn = conn  # type: ignore[assignment]
    candidate = LeaderboardCandidate(
        address="0x" + "b" * 40,
        display_name=None,
        account_value=100_000.0,
        windows={"month": WindowPerformance(pnl=1_000, roi=0.1, volume=1_000_000)},
        raw={"ignored": True},
    )
    next_ms = int((latest + timedelta(hours=1)).timestamp() * 1_000)

    asyncio.run(db.upsert_leaderboard([candidate], next_ms, snapshot_min_interval_minutes=240))

    assert any("INSERT INTO wallets" in query for query, _ in conn.calls)
    assert not any("INSERT INTO leaderboard_snapshots" in query for query, _ in conn.calls)
