from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PG_PORT = "5433"
PG_DB = "hlcopy"
DATA_MOUNT = Path("/mnt/HC_Volume_106576526")
DEFAULT_PLAN = Path("/root/hyperliquid-audit/postgres-compaction/plan.json")
AUDIT_LOG = Path("/root/hyperliquid-audit/postgres-compaction/apply.json")
HISTORICAL_BIN_HOURS = 8
MIN_AVAILABLE_BYTES = 3 * 1024**3
MAX_POST_CUTOFF_ROWS = 500_000


def _psql(sql: str) -> str:
    result = subprocess.run(
        [
            "sudo",
            "-n",
            "-u",
            "postgres",
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-p",
            PG_PORT,
            "-d",
            PG_DB,
            "-At",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _scalar(sql: str) -> str:
    value = _psql(sql).splitlines()
    if len(value) != 1:
        raise RuntimeError(f"expected one scalar row, got {len(value)}")
    return value[0]


def _int(sql: str) -> int:
    return int(_scalar(sql))


def _available_bytes() -> int:
    stat = os.statvfs(DATA_MOUNT)
    return stat.f_bavail * stat.f_frsize


def _relation_bytes(name: str) -> int:
    return _int(f"SELECT pg_total_relation_size('{name}'::regclass)")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_cutoff(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def build_plan(path: Path) -> dict[str, Any]:
    cutoff = _scalar("SELECT max(snapshot_at)::text FROM leaderboard_snapshots")
    if not cutoff:
        raise RuntimeError("leaderboard_snapshots has no cutoff")
    cutoff_dt = _parse_cutoff(cutoff)
    cutoff_sql = cutoff_dt.isoformat()
    source_rows = _int(
        "SELECT count(*) FROM leaderboard_snapshots "
        f"WHERE snapshot_at <= TIMESTAMPTZ '{cutoff_sql}'"
    )
    keep_times = _int(
        "WITH keep_times AS ("
        " SELECT max(snapshot_at) snapshot_at"
        " FROM (SELECT DISTINCT snapshot_at FROM leaderboard_snapshots"
        f"       WHERE snapshot_at <= TIMESTAMPTZ '{cutoff_sql}') t"
        f" GROUP BY date_bin('{HISTORICAL_BIN_HOURS} hours', snapshot_at,"
        " TIMESTAMPTZ '2000-01-01 00:00:00+00')"
        ") SELECT count(*) FROM keep_times"
    )
    keep_rows = _int(
        "WITH keep_times AS ("
        " SELECT max(snapshot_at) snapshot_at"
        " FROM (SELECT DISTINCT snapshot_at FROM leaderboard_snapshots"
        f"       WHERE snapshot_at <= TIMESTAMPTZ '{cutoff_sql}') t"
        f" GROUP BY date_bin('{HISTORICAL_BIN_HOURS} hours', snapshot_at,"
        " TIMESTAMPTZ '2000-01-01 00:00:00+00')"
        ") SELECT count(*) FROM leaderboard_snapshots l"
        " JOIN keep_times k USING(snapshot_at)"
    )
    raw_api_rows = _int("SELECT count(*) FROM raw_api_responses")
    raw_api_unique_payloads = _int(
        "SELECT count(DISTINCT content_sha256) FROM raw_api_responses"
    )
    leaderboard_raw_observations = _int(
        "SELECT count(*) FROM raw_api_responses WHERE endpoint='leaderboard'"
    )
    exact_cutoff_raw = _int(
        "SELECT count(*) FROM raw_api_responses WHERE endpoint='leaderboard' "
        f"AND fetched_at = TIMESTAMPTZ '{cutoff_sql}'"
    )
    if leaderboard_raw_observations < 1 or exact_cutoff_raw < 1:
        raise RuntimeError("raw leaderboard provenance does not cover the compaction cutoff")
    plan: dict[str, Any] = {
        "schema_version": 1,
        "mode": "REVIEW_ONLY_NO_MUTATION",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": {"port": 5433, "name": PG_DB},
        "policy": {
            "historical_leaderboard_bin_hours": HISTORICAL_BIN_HOURS,
            "keep_all_rows_after_cutoff": True,
            "leaderboard_raw_json_reconstructable_from_raw_api_payloads": True,
            "raw_api_observations_preserved": True,
            "raw_api_payloads_content_addressed": True,
            "fills_untouched": True,
        },
        "leaderboard": {
            "cutoff": cutoff_dt.isoformat(),
            "source_rows_through_cutoff": source_rows,
            "keep_snapshot_times_through_cutoff": keep_times,
            "keep_rows_through_cutoff": keep_rows,
            "source_relation_bytes": _relation_bytes("leaderboard_snapshots"),
            "raw_api_observations": leaderboard_raw_observations,
            "exact_cutoff_raw_observations": exact_cutoff_raw,
        },
        "raw_api": {
            "observation_rows": raw_api_rows,
            "unique_payloads": raw_api_unique_payloads,
            "source_relation_bytes": _relation_bytes("raw_api_responses"),
        },
        "fills": {
            "rows": _int("SELECT count(*) FROM fills"),
            "relation_bytes": _relation_bytes("fills"),
        },
        "available_bytes": _available_bytes(),
        "polymarket_mutation": False,
        "real_trading_change": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    print(json.dumps(plan, indent=2, sort_keys=True))
    print(f"PLAN={path}")
    print(f"PLAN_SHA256={_sha256(path)}")
    print("DATABASE_MUTATION=NO")
    print("POLYMARKET_MUTATION=NO")
    print("REAL_TRADING_CHANGE=NO")
    return plan


def _validate_plan(path: Path, expected_sha256: str) -> dict[str, Any]:
    actual = _sha256(path)
    if actual != expected_sha256:
        raise RuntimeError(f"manifest hash mismatch: {actual}")
    plan = json.loads(path.read_text())
    if plan.get("mode") != "REVIEW_ONLY_NO_MUTATION":
        raise RuntimeError("unexpected plan mode")
    if plan.get("polymarket_mutation") is not False:
        raise RuntimeError("Polymarket boundary missing")
    if plan.get("real_trading_change") is not False:
        raise RuntimeError("real trading boundary missing")
    cutoff = _parse_cutoff(str(plan["leaderboard"]["cutoff"]))
    if int(plan["policy"]["historical_leaderboard_bin_hours"]) != HISTORICAL_BIN_HOURS:
        raise RuntimeError("reviewed bin policy mismatch")
    if not bool(plan["policy"]["fills_untouched"]):
        raise RuntimeError("fills must remain protected")
    plan["_cutoff_dt"] = cutoff
    return plan


def _compact_leaderboard(plan: dict[str, Any]) -> None:
    cutoff = plan["_cutoff_dt"].isoformat()
    expected_source = int(plan["leaderboard"]["source_rows_through_cutoff"])
    current_source = _int(
        "SELECT count(*) FROM leaderboard_snapshots "
        f"WHERE snapshot_at <= TIMESTAMPTZ '{cutoff}'"
    )
    if current_source != expected_source:
        raise RuntimeError(
            f"leaderboard source changed through cutoff: {current_source} != {expected_source}"
        )
    post_cutoff = _int(
        "SELECT count(*) FROM leaderboard_snapshots "
        f"WHERE snapshot_at > TIMESTAMPTZ '{cutoff}'"
    )
    if post_cutoff > MAX_POST_CUTOFF_ROWS:
        raise RuntimeError(f"too many unreviewed post-cutoff rows: {post_cutoff}")
    expected_keep = int(plan["leaderboard"]["keep_rows_through_cutoff"]) + post_cutoff

    sql = f"""
BEGIN;
LOCK TABLE leaderboard_snapshots IN ACCESS EXCLUSIVE MODE;
DROP TABLE IF EXISTS leaderboard_snapshots_compact_v1;
CREATE TABLE leaderboard_snapshots_compact_v1 (
    snapshot_at TIMESTAMPTZ NOT NULL,
    address TEXT NOT NULL REFERENCES wallets(address),
    ranking_period TEXT NOT NULL,
    rank INTEGER,
    pnl NUMERIC,
    roi NUMERIC,
    volume NUMERIC,
    account_value NUMERIC,
    raw_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    PRIMARY KEY (snapshot_at, address, ranking_period)
);
WITH keep_times AS (
    SELECT max(snapshot_at) AS snapshot_at
    FROM (
        SELECT DISTINCT snapshot_at
        FROM leaderboard_snapshots
        WHERE snapshot_at <= TIMESTAMPTZ '{cutoff}'
    ) t
    GROUP BY date_bin(
        '{HISTORICAL_BIN_HOURS} hours', snapshot_at,
        TIMESTAMPTZ '2000-01-01 00:00:00+00'
    )
)
INSERT INTO leaderboard_snapshots_compact_v1(
    snapshot_at,address,ranking_period,rank,pnl,roi,volume,account_value,raw_json
)
SELECT l.snapshot_at,l.address,l.ranking_period,l.rank,l.pnl,l.roi,l.volume,
       l.account_value,'{{}}'::jsonb
FROM leaderboard_snapshots l
JOIN keep_times k USING(snapshot_at)
UNION ALL
SELECT snapshot_at,address,ranking_period,rank,pnl,roi,volume,account_value,'{{}}'::jsonb
FROM leaderboard_snapshots
WHERE snapshot_at > TIMESTAMPTZ '{cutoff}';
CREATE INDEX idx_leaderboard_snapshots_compact_v1_address_time
    ON leaderboard_snapshots_compact_v1(address, snapshot_at DESC);
DO $verify$
BEGIN
  IF (SELECT count(*) FROM leaderboard_snapshots_compact_v1) <> {expected_keep} THEN
    RAISE EXCEPTION 'compact row-count mismatch';
  END IF;
  IF (SELECT max(snapshot_at) FROM leaderboard_snapshots_compact_v1)
     IS DISTINCT FROM (SELECT max(snapshot_at) FROM leaderboard_snapshots) THEN
    RAISE EXCEPTION 'compact latest snapshot mismatch';
  END IF;
END
$verify$;
ALTER TABLE leaderboard_snapshots RENAME TO leaderboard_snapshots_old_v1;
ALTER TABLE leaderboard_snapshots_compact_v1 RENAME TO leaderboard_snapshots;
DROP TABLE leaderboard_snapshots_old_v1;
ALTER TABLE leaderboard_snapshots
    RENAME CONSTRAINT leaderboard_snapshots_compact_v1_pkey TO leaderboard_snapshots_pkey;
ALTER INDEX idx_leaderboard_snapshots_compact_v1_address_time
    RENAME TO idx_leaderboard_snapshots_address_time;
COMMIT;
ANALYZE leaderboard_snapshots;
"""
    _psql(sql)


def _normalize_raw_api_payloads() -> None:
    _psql(
        """
CREATE TABLE IF NOT EXISTS raw_api_payloads (
    content_sha256 TEXT PRIMARY KEY,
    response_json JSONB NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL
);
INSERT INTO raw_api_payloads(content_sha256,response_json,first_seen)
SELECT DISTINCT ON(content_sha256) content_sha256,response_json,fetched_at
FROM raw_api_responses
WHERE response_json <> '{}'::jsonb
ORDER BY content_sha256,fetched_at
ON CONFLICT(content_sha256) DO NOTHING;
"""
    )
    missing = _int(
        "SELECT count(*) FROM raw_api_responses r "
        "WHERE NOT EXISTS (SELECT 1 FROM raw_api_payloads p "
        "WHERE p.content_sha256=r.content_sha256)"
    )
    if missing:
        raise RuntimeError(f"raw API payload coverage missing for {missing} observations")
    _psql("UPDATE raw_api_responses SET response_json='{}'::jsonb WHERE response_json <> '{}'::jsonb")
    _psql("VACUUM (FULL, ANALYZE) raw_api_responses")
    _psql(
        "CREATE INDEX IF NOT EXISTS idx_raw_api_responses_content_sha256 "
        "ON raw_api_responses(content_sha256)"
    )
    _psql("ANALYZE raw_api_payloads")


def apply_plan(path: Path, expected_sha256: str) -> None:
    plan = _validate_plan(path, expected_sha256)
    before_available = _available_bytes()
    if before_available < MIN_AVAILABLE_BYTES:
        raise RuntimeError(f"insufficient preflight headroom: {before_available}")
    subprocess.run(
        ["sudo", "-n", "-u", "postgres", "/usr/lib/postgresql/14/bin/pg_isready", "-p", PG_PORT],
        check=True,
    )
    fills_before = _int("SELECT count(*) FROM fills")
    if fills_before != int(plan["fills"]["rows"]):
        raise RuntimeError("fills changed since reviewed plan; refusing compaction")

    audit: dict[str, Any] = {
        "manifest_sha256": expected_sha256,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "before_available_bytes": before_available,
        "fills_before": fills_before,
        "leaderboard_compaction_completed": False,
        "raw_api_normalization_completed": False,
        "polymarket_mutation": False,
        "real_trading_change": False,
    }
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        _compact_leaderboard(plan)
        audit["leaderboard_compaction_completed"] = True
        audit["after_leaderboard_available_bytes"] = _available_bytes()
        _normalize_raw_api_payloads()
        audit["raw_api_normalization_completed"] = True
        fills_after = _int("SELECT count(*) FROM fills")
        if fills_after != fills_before:
            raise RuntimeError("fills row count changed during compaction")
        audit.update(
            {
                "fills_after": fills_after,
                "leaderboard_relation_bytes_after": _relation_bytes("leaderboard_snapshots"),
                "raw_api_observations_bytes_after": _relation_bytes("raw_api_responses"),
                "raw_api_payloads_bytes_after": _relation_bytes("raw_api_payloads"),
                "after_available_bytes": _available_bytes(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "success": True,
            }
        )
    except Exception as exc:
        audit.update(
            {
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
                "after_available_bytes": _available_bytes(),
                "success": False,
            }
        )
        raise
    finally:
        AUDIT_LOG.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
        print(json.dumps(audit, indent=2, sort_keys=True))
        print(f"AUDIT_LOG={AUDIT_LOG}")
        print("POLYMARKET_MUTATION=NO")
        print("REAL_TRADING_CHANGE=NO")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-output", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        if args.manifest is None or not args.expected_sha256:
            raise SystemExit("--apply requires --manifest and --expected-sha256")
        apply_plan(args.manifest, args.expected_sha256)
    else:
        build_plan(args.plan_output)


if __name__ == "__main__":
    main()
