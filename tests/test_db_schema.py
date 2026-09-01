from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hlcopy.db.postgres import Database
from hlcopy.discovery.leaderboard import LeaderboardCandidate, WindowPerformance

psycopg = pytest.importorskip("psycopg")


def _apply_schema(conn) -> None:
    schema = Path("src/hlcopy/db/schema.sql").read_text()
    for statement in schema.split(";"):
        if statement.strip():
            conn.execute(statement)


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="PostgreSQL not configured")
def test_schema_applies_idempotently():
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        for _ in range(2):
            _apply_schema(conn)
        exists = conn.execute(
            """
            SELECT count(*)
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'fills'
            """
        ).fetchone()
        assert exists is not None
        assert exists[0] == 1


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="PostgreSQL not configured")
def test_position_episode_accepts_large_fill_tid_array():
    wallet = "0x" + "a" * 40
    tids = list(range(1, 1001))
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        _apply_schema(conn)
        conn.execute(
            """
            INSERT INTO wallets(address, first_seen, last_seen, source, metadata_json)
            VALUES (%s, now(), now(), 'test', '{}'::jsonb)
            ON CONFLICT(address) DO NOTHING
            """,
            (wallet,),
        )
        conn.execute("DELETE FROM position_episodes WHERE wallet_address = %s", (wallet,))
        conn.execute(
            """
            INSERT INTO position_episodes(
              wallet_address, coin, direction, opened_at, closed_at, avg_entry, avg_exit,
              max_size, realized_pnl, fees, funding, holding_seconds, complete_start,
              fill_count, fill_tids
            )
            VALUES (%s, 'BTC', 'LONG', now(), now(), 100, 101, 10, 1, 0, 0, 1, true, %s, %s)
            """,
            (wallet, len(tids), tids),
        )
        stored = conn.execute(
            "SELECT cardinality(fill_tids) FROM position_episodes WHERE wallet_address = %s",
            (wallet,),
        ).fetchone()
        assert stored == (1000,)
        conn.execute("DELETE FROM position_episodes WHERE wallet_address = %s", (wallet,))
        conn.execute("DELETE FROM wallets WHERE address = %s", (wallet,))


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="PostgreSQL not configured")
def test_partial_leaderboard_snapshot_rolls_back_and_retries() -> None:
    async def scenario() -> None:
        snapshot_at = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
        snapshot_ms = int(snapshot_at.timestamp() * 1_000)
        first = "0x" + "c" * 40
        second = "0x" + "d" * 40
        candidates = [
            LeaderboardCandidate(
                address=first,
                display_name="first",
                account_value=100_000.0,
                windows={"month": WindowPerformance(pnl=2_000, roi=0.2, volume=1_000_000)},
                raw={},
            ),
            LeaderboardCandidate(
                address=second,
                display_name="second",
                account_value=90_000.0,
                windows={"month": WindowPerformance(pnl=1_000, roi=0.1, volume=900_000)},
                raw={},
            ),
        ]
        trigger_name = "hlcopy_test_fail_partial_snapshot"
        function_name = "hlcopy_test_fail_partial_snapshot_fn"

        async with Database(os.environ["DATABASE_URL"]) as db:
            await db.init_schema()
            conn = db._require()
            await conn.execute(
                "DELETE FROM leaderboard_snapshots WHERE snapshot_at = %s",
                (snapshot_at,),
            )
            await conn.execute("DELETE FROM wallets WHERE address IN (%s, %s)", (first, second))
            await conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON leaderboard_snapshots")
            await conn.execute(f"DROP FUNCTION IF EXISTS {function_name}()")
            await conn.execute(
                f"""
                CREATE FUNCTION {function_name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.snapshot_at = '{snapshot_at.isoformat()}'::timestamptz
                       AND NEW.address = '{second}' THEN
                        RAISE EXCEPTION 'injected mid-snapshot failure';
                    END IF;
                    RETURN NEW;
                END
                $$
                """
            )
            await conn.execute(
                f"""
                CREATE TRIGGER {trigger_name}
                BEFORE INSERT ON leaderboard_snapshots
                FOR EACH ROW EXECUTE FUNCTION {function_name}()
                """
            )
            try:
                with pytest.raises(psycopg.errors.RaiseException):
                    await db.upsert_leaderboard(
                        candidates,
                        snapshot_ms,
                        snapshot_min_interval_minutes=240,
                    )

                cursor = await conn.execute(
                    "SELECT count(*) FROM leaderboard_snapshots WHERE snapshot_at = %s",
                    (snapshot_at,),
                )
                assert await cursor.fetchone() == (0,)
            finally:
                await conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON leaderboard_snapshots")
                await conn.execute(f"DROP FUNCTION IF EXISTS {function_name}()")

            await db.upsert_leaderboard(
                candidates,
                snapshot_ms,
                snapshot_min_interval_minutes=240,
            )
            cursor = await conn.execute(
                "SELECT count(*) FROM leaderboard_snapshots WHERE snapshot_at = %s",
                (snapshot_at,),
            )
            assert await cursor.fetchone() == (2,)

            await conn.execute(
                "DELETE FROM leaderboard_snapshots WHERE snapshot_at = %s",
                (snapshot_at,),
            )
            await conn.execute("DELETE FROM wallets WHERE address IN (%s, %s)", (first, second))

    asyncio.run(scenario())
