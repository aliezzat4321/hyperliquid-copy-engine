from __future__ import annotations

import asyncio

from hlcopy.db.postgres import Database
from hlcopy.discovery.leaderboard import LeaderboardCandidate, WindowPerformance


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def execute(self, query: str, params: object = None) -> None:
        self.calls.append((query, params))


def test_store_raw_preserves_request_identity_while_suppressing_exact_repeat() -> None:
    db = Database("unused")
    conn = _FakeConn()
    db.conn = conn  # type: ignore[assignment]

    asyncio.run(
        db.store_raw(
            source="hyperliquid",
            endpoint="userFills",
            request_payload={"type": "userFills", "user": "0xabc"},
            response_payload=[],
            fetched_at_ms=1_000,
        )
    )

    query, params = conn.calls[-1]
    assert "WHERE NOT EXISTS" in query
    assert "request_json IS NOT DISTINCT FROM" in query
    assert "content_sha256" in query
    assert "0xabc" in repr(params)


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
