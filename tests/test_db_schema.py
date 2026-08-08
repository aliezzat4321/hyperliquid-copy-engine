from __future__ import annotations

import os
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="PostgreSQL not configured")
def test_schema_applies_idempotently():
    schema = Path("src/hlcopy/db/schema.sql").read_text()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        for _ in range(2):
            for statement in schema.split(";"):
                if statement.strip():
                    conn.execute(statement)
        exists = conn.execute(
            """
            SELECT count(*)
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'fills'
            """
        ).fetchone()
        assert exists is not None
        assert exists[0] == 1
