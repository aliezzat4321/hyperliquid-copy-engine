from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "postgres_storage_compact_v1.py"
SPEC = importlib.util.spec_from_file_location("postgres_storage_compact_v1_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _install_schema_psql(monkeypatch: pytest.MonkeyPatch, schema: str) -> None:
    database_url = os.environ["DATABASE_URL"]

    def run(sql: str) -> str:
        with psycopg.connect(database_url, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}", public')
                cursor.execute(sql, prepare=False)
                if cursor.description is None:
                    return ""
                rows = cursor.fetchall()
                return "\n".join(
                    "|".join(
                        ""
                        if value is None
                        else json.dumps(value)
                        if isinstance(value, (dict, list))
                        else str(value)
                        for value in row
                    )
                    for row in rows
                )

    monkeypatch.setattr(MODULE, "_psql", run)


def _schema() -> str:
    return f"hlcopy_compact_test_{uuid.uuid4().hex}"


def _create_schema(schema: str) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')


def _drop_schema(schema: str) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="PostgreSQL not configured")
def test_leaderboard_structure_accepts_canonical_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = _schema()
    _create_schema(schema)
    try:
        database_url = os.environ["DATABASE_URL"]
        schema_sql = (
            Path(__file__).resolve().parents[1] / "src" / "hlcopy" / "db" / "schema.sql"
        ).read_text()
        with psycopg.connect(database_url, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}", public')
                cursor.execute(schema_sql, prepare=False)

        _install_schema_psql(monkeypatch, schema)
        structure = MODULE._leaderboard_structure()

        # pg_get_indexdef(index_oid, column_no, pretty) returns the key expression
        # without its ordering flags. The drift guard must reconstruct DESC from
        # pg_index.indoption instead of expecting pg_get_indexdef to include it.
        assert structure["indexes"] == [
            [
                "idx_leaderboard_snapshots_address_time",
                False,
                False,
                True,
                ["address", "snapshot_at DESC"],
            ],
            [
                "leaderboard_snapshots_pkey",
                True,
                True,
                True,
                ["snapshot_at", "address", "ranking_period"],
            ],
        ]
        assert structure["constraints"] == MODULE.EXPECTED_LEADERBOARD_CONSTRAINTS
        assert [
            "leaderboard_snapshots_address_fkey",
            "f",
            True,
            False,
            False,
            "FOREIGN KEY (address) REFERENCES wallets(address)",
        ] in structure["constraints"]
    finally:
        _drop_schema(schema)


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="PostgreSQL not configured")
def test_plan_rejects_missing_provenance_for_discarded_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    schema = _schema()
    _create_schema(schema)
    try:
        _install_schema_psql(monkeypatch, schema)
        MODULE._psql(
            f"""
CREATE TABLE leaderboard_snapshots (
    snapshot_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE raw_api_responses (
    endpoint TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    content_sha256 TEXT NOT NULL,
    response_json JSONB NOT NULL
);
INSERT INTO leaderboard_snapshots(snapshot_at) VALUES
    ('2026-08-18 00:00:00+00'),
    ('2026-08-18 01:00:00+00'),
    ('2026-08-18 09:00:00+00');
INSERT INTO raw_api_responses(endpoint,fetched_at,content_sha256,response_json) VALUES
    ('leaderboard','2026-08-18 01:00:00+00','{MODULE.EMPTY_PAYLOAD_SHA256}',
     '{{}}'::jsonb),
    ('leaderboard','2026-08-18 09:00:00+00','{MODULE.EMPTY_PAYLOAD_SHA256}',
     '{{}}'::jsonb);
"""
        )

        plan_path = tmp_path / "plan.json"
        with pytest.raises(
            RuntimeError,
            match="discarded leaderboard snapshots missing exact raw provenance: 1",
        ):
            MODULE.build_plan(plan_path)
        assert not plan_path.exists()
    finally:
        _drop_schema(schema)


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="PostgreSQL not configured")
def test_raw_api_hash_conflict_rolls_back_without_clearing_source_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = _schema()
    _create_schema(schema)
    try:
        _install_schema_psql(monkeypatch, schema)
        monkeypatch.setattr(MODULE, "_available_bytes", lambda: 10 * 1024**3)
        MODULE._psql(
            """
CREATE TABLE raw_api_responses (
    content_sha256 TEXT NOT NULL,
    response_json JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL
);
INSERT INTO raw_api_responses(content_sha256,response_json,fetched_at) VALUES
    ('same-hash','{"value":1}'::jsonb,'2026-08-18 00:00:00+00'),
    ('same-hash','{"value":2}'::jsonb,'2026-08-18 00:01:00+00');
"""
        )
        plan = {
            "raw_api": {
                "observation_rows": 2,
                "required_peak_available_bytes": 0,
            }
        }

        with pytest.raises(Exception, match="source observations disagree"):
            MODULE._normalize_raw_api_payloads(plan)

        assert MODULE._int(
            "SELECT count(*) FROM raw_api_responses WHERE response_json <> '{}'::jsonb"
        ) == 2
        assert MODULE._int(
            "SELECT count(DISTINCT response_json) FROM raw_api_responses"
        ) == 2
        assert MODULE._int(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            f"WHERE n.nspname='{schema}' AND c.relname='raw_api_payloads'"
        ) == 0
    finally:
        _drop_schema(schema)


def test_raw_api_normalization_seeds_empty_payload_and_vacuums_before_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []
    row_counts = iter((2, 0))

    def fake_psql(sql: str) -> str:
        statements.append(sql)
        if sql.startswith("SELECT count(*) FROM raw_api_responses"):
            return str(next(row_counts))
        return ""

    monkeypatch.setattr(MODULE, "_psql", fake_psql)
    monkeypatch.setattr(MODULE, "_available_bytes", lambda: 10 * 1024**3)
    MODULE._normalize_raw_api_payloads({
        "raw_api": {"observation_rows": 2, "required_peak_available_bytes": 1}
    })
    transaction = statements[1]
    assert MODULE.EMPTY_PAYLOAD_SHA256 in transaction
    assert "SELECT content_sha256,'{}'::jsonb,min(fetched_at)" in transaction
    assert statements.index("VACUUM raw_api_responses") < statements.index(
        "VACUUM (FULL, ANALYZE) raw_api_responses"
    )
