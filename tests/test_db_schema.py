from __future__ import annotations

import os
from pathlib import Path

import pytest

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
