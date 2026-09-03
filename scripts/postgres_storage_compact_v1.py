from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from hlcopy.storage.metrics import disk_usage  # noqa: E402

PG_PORT = "5433"
PG_DB = "hlcopy"
DATA_MOUNT = Path("/mnt/HC_Volume_106576526")
DEFAULT_PLAN = Path("/root/hyperliquid-audit/postgres-compaction/plan.json")
AUDIT_LOG = Path("/root/hyperliquid-audit/postgres-compaction/apply.json")
HISTORICAL_BIN_HOURS = 8
MIN_AVAILABLE_BYTES = 3 * 1024**3
MAX_POST_CUTOFF_ROWS = 500_000
MAX_RAW_API_POST_PLAN_ROWS = 100_000
MAX_PLAN_AGE_HOURS = 12
FIXED_PEAK_MARGIN_BYTES = 512 * 1024**2
RAW_PAYLOAD_OVERHEAD_FACTOR = 1.35
LEADERBOARD_RELATION_OVERHEAD_FACTOR = 1.50
LEADERBOARD_WAL_FACTOR = 1.50
UTC = timezone(timedelta(0))


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


def _float(sql: str) -> float:
    return float(_scalar(sql))


def _available_bytes() -> int:
    return disk_usage(DATA_MOUNT).available


def _relation_bytes(name: str) -> int:
    return _int(f"SELECT pg_total_relation_size('{name}'::regclass)")


def _relation_exists(name: str) -> bool:
    return bool(
        _int(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            f"WHERE n.nspname='public' AND c.relname='{name}'"
        )
    )


def _optional_relation_bytes(name: str) -> int:
    return _relation_bytes(name) if _relation_exists(name) else 0


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    cleaned = value.strip().replace(" ", "T")
    if cleaned.endswith(("+00", "-00")):
        cleaned += ":00"
    parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _write_audit(audit: dict[str, Any]) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    temporary = AUDIT_LOG.with_suffix(".tmp")
    with temporary.open("w") as handle:
        handle.write(json.dumps(audit, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(AUDIT_LOG)


def _active_other_client_sessions() -> int:
    return _int(
        "SELECT count(*) FROM pg_stat_activity "
        f"WHERE datname='{PG_DB}' AND pid <> pg_backend_pid() "
        "AND backend_type='client backend'"
    )


def _kept_snapshot_sql(cutoff_sql: str) -> str:
    return (
        "WITH keep_times AS ("
        " SELECT max(snapshot_at) snapshot_at"
        " FROM (SELECT DISTINCT snapshot_at FROM leaderboard_snapshots"
        f"       WHERE snapshot_at <= TIMESTAMPTZ '{cutoff_sql}') t"
        f" GROUP BY date_bin('{HISTORICAL_BIN_HOURS} hours', snapshot_at,"
        " TIMESTAMPTZ '2000-01-01 00:00:00+00')"
        ") "
    )


def _missing_discarded_provenance(cutoff_sql: str) -> int:
    keep_prefix = _kept_snapshot_sql(cutoff_sql)
    return _int(
        keep_prefix
        + "SELECT count(*) FROM ("
        " SELECT DISTINCT snapshot_at FROM leaderboard_snapshots"
        f" WHERE snapshot_at <= TIMESTAMPTZ '{cutoff_sql}'"
        ") d LEFT JOIN keep_times k USING(snapshot_at)"
        " WHERE k.snapshot_at IS NULL AND NOT EXISTS ("
        " SELECT 1 FROM raw_api_responses r"
        " WHERE r.endpoint='leaderboard' AND r.fetched_at=d.snapshot_at)"
    )


def _source_payload_conflicts() -> int:
    return _int(
        "SELECT count(*) FROM ("
        " SELECT content_sha256 FROM raw_api_responses"
        " WHERE response_json <> '{}'::jsonb"
        " GROUP BY content_sha256"
        " HAVING count(DISTINCT response_json) > 1"
        ") conflicts"
    )


def build_plan(path: Path) -> dict[str, Any]:
    cutoff = _scalar("SELECT max(snapshot_at)::text FROM leaderboard_snapshots")
    if not cutoff:
        raise RuntimeError("leaderboard_snapshots has no cutoff")
    cutoff_dt = _parse_timestamp(cutoff)
    cutoff_sql = cutoff_dt.isoformat()
    source_rows = _int(
        "SELECT count(*) FROM leaderboard_snapshots "
        f"WHERE snapshot_at <= TIMESTAMPTZ '{cutoff_sql}'"
    )
    keep_prefix = _kept_snapshot_sql(cutoff_sql)
    keep_times = _int(keep_prefix + "SELECT count(*) FROM keep_times")
    keep_rows = _int(
        keep_prefix
        + "SELECT count(*) FROM leaderboard_snapshots l "
        "JOIN keep_times k USING(snapshot_at)"
    )
    if keep_rows < 1 or keep_rows >= source_rows:
        raise RuntimeError(
            f"invalid compaction ratio: keep_rows={keep_rows} source_rows={source_rows}"
        )

    missing_retained_provenance = _int(
        keep_prefix
        + "SELECT count(*) FROM keep_times k WHERE NOT EXISTS ("
        " SELECT 1 FROM raw_api_responses r"
        " WHERE r.endpoint='leaderboard' AND r.fetched_at=k.snapshot_at)"
    )
    if missing_retained_provenance:
        raise RuntimeError(
            "retained leaderboard snapshots missing exact raw provenance: "
            f"{missing_retained_provenance}"
        )
    missing_discarded_provenance = _missing_discarded_provenance(cutoff_sql)
    if missing_discarded_provenance:
        raise RuntimeError(
            "discarded leaderboard snapshots missing exact raw provenance: "
            f"{missing_discarded_provenance}"
        )

    raw_api_rows = _int("SELECT count(*) FROM raw_api_responses")
    raw_api_unique_payloads = _int(
        "SELECT count(DISTINCT content_sha256) FROM raw_api_responses"
    )
    raw_api_unique_body_bytes = _int(
        "SELECT COALESCE(sum(payload_bytes),0)::bigint FROM ("
        " SELECT DISTINCT ON(content_sha256) content_sha256,"
        " pg_column_size(response_json)::bigint payload_bytes"
        " FROM raw_api_responses WHERE response_json <> '{}'::jsonb"
        " ORDER BY content_sha256,fetched_at) payloads"
    )
    source_payload_conflicts = _source_payload_conflicts()
    if source_payload_conflicts:
        raise RuntimeError(
            "raw API source rows disagree on canonical content hash: "
            f"{source_payload_conflicts}"
        )

    existing_payload_conflicts = 0
    if _relation_exists("raw_api_payloads"):
        existing_payload_conflicts = _int(
            "SELECT count(*) FROM raw_api_responses r JOIN raw_api_payloads p"
            " ON p.content_sha256=r.content_sha256"
            " WHERE r.response_json <> '{}'::jsonb"
            " AND p.response_json IS DISTINCT FROM r.response_json"
        )
    if existing_payload_conflicts:
        raise RuntimeError(
            f"existing content-addressed payloads disagree: {existing_payload_conflicts}"
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

    source_relation_bytes = _relation_bytes("leaderboard_snapshots")
    raw_api_relation_bytes = _relation_bytes("raw_api_responses")
    existing_payload_relation_bytes = _optional_relation_bytes("raw_api_payloads")
    sample_avg_raw_json_bytes = _float(
        "SELECT COALESCE(avg(pg_column_size(raw_json)),0) "
        "FROM leaderboard_snapshots TABLESAMPLE SYSTEM (0.25)"
    )
    raw_json_estimate = min(
        source_relation_bytes,
        int(math.ceil(source_rows * sample_avg_raw_json_bytes)),
    )
    non_raw_source_bytes = max(1, source_relation_bytes - raw_json_estimate)
    keep_ratio = keep_rows / source_rows
    compact_relation_estimate = int(
        math.ceil(non_raw_source_bytes * keep_ratio * LEADERBOARD_RELATION_OVERHEAD_FACTOR)
    )
    leaderboard_wal_reserve = int(
        math.ceil(compact_relation_estimate * LEADERBOARD_WAL_FACTOR)
    )
    leaderboard_peak_required = (
        compact_relation_estimate + leaderboard_wal_reserve + FIXED_PEAK_MARGIN_BYTES
    )

    payload_relation_estimate = max(
        existing_payload_relation_bytes,
        int(math.ceil(raw_api_unique_body_bytes * RAW_PAYLOAD_OVERHEAD_FACTOR)),
    )
    raw_observation_after_estimate = max(64 * 1024**2, raw_api_rows * 2_048)
    raw_api_phase_required = max(
        MIN_AVAILABLE_BYTES,
        max(0, payload_relation_estimate - existing_payload_relation_bytes)
        + raw_observation_after_estimate
        + FIXED_PEAK_MARGIN_BYTES,
    )
    projected_raw_api_net_reclaim = max(
        0,
        raw_api_relation_bytes
        - raw_observation_after_estimate
        - max(0, payload_relation_estimate - existing_payload_relation_bytes),
    )
    available = _available_bytes()
    projected_after_raw_api = available + projected_raw_api_net_reclaim
    if available < raw_api_phase_required:
        raise RuntimeError(
            "raw API normalization peak is not feasible: "
            f"available={available} required={raw_api_phase_required}"
        )
    if projected_after_raw_api < leaderboard_peak_required:
        raise RuntimeError(
            "projected post-dedupe headroom cannot safely rebuild leaderboard: "
            f"projected={projected_after_raw_api} required={leaderboard_peak_required}"
        )

    plan: dict[str, Any] = {
        "schema_version": 3,
        "mode": "REVIEW_ONLY_NO_MUTATION",
        "generated_at": datetime.now(UTC).isoformat(),
        "database": {"port": int(PG_PORT), "name": PG_DB},
        "policy": {
            "historical_leaderboard_bin_hours": HISTORICAL_BIN_HOURS,
            "keep_all_rows_after_cutoff": True,
            "all_discarded_snapshot_times_require_raw_provenance": True,
            "leaderboard_raw_json_reconstructable_from_raw_api_payloads": True,
            "raw_api_observations_preserved": True,
            "raw_api_payloads_content_addressed": True,
            "source_hash_identity_must_be_unique": True,
            "content_sha256_is_canonical_payload_identity": True,
            "fills_untouched": True,
            "raw_api_first_for_headroom": True,
            "unnecessary_observation_hash_index": False,
        },
        "leaderboard": {
            "cutoff": cutoff_dt.isoformat(),
            "source_rows_through_cutoff": source_rows,
            "keep_snapshot_times_through_cutoff": keep_times,
            "keep_rows_through_cutoff": keep_rows,
            "source_relation_bytes": source_relation_bytes,
            "sample_avg_raw_json_bytes": sample_avg_raw_json_bytes,
            "estimated_compact_relation_bytes": compact_relation_estimate,
            "estimated_wal_reserve_bytes": leaderboard_wal_reserve,
            "required_peak_available_bytes": leaderboard_peak_required,
            "raw_api_observations": leaderboard_raw_observations,
            "exact_cutoff_raw_observations": exact_cutoff_raw,
            "missing_retained_provenance": missing_retained_provenance,
            "missing_discarded_provenance": missing_discarded_provenance,
        },
        "raw_api": {
            "observation_rows": raw_api_rows,
            "unique_payloads": raw_api_unique_payloads,
            "unique_body_bytes": raw_api_unique_body_bytes,
            "source_relation_bytes": raw_api_relation_bytes,
            "existing_payload_relation_bytes": existing_payload_relation_bytes,
            "estimated_payload_relation_bytes": payload_relation_estimate,
            "estimated_observation_relation_bytes_after": raw_observation_after_estimate,
            "required_peak_available_bytes": raw_api_phase_required,
            "projected_net_reclaim_bytes": projected_raw_api_net_reclaim,
            "source_payload_conflicts": source_payload_conflicts,
            "existing_payload_conflicts": existing_payload_conflicts,
        },
        "fills": {
            "rows_at_plan": _int("SELECT count(*) FROM fills"),
            "relation_bytes": _relation_bytes("fills"),
        },
        "available_bytes": available,
        "projected_available_after_raw_api_bytes": projected_after_raw_api,
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
    if int(plan.get("schema_version", 0)) != 3:
        raise RuntimeError("unexpected plan schema")
    if plan.get("mode") != "REVIEW_ONLY_NO_MUTATION":
        raise RuntimeError("unexpected plan mode")
    if plan.get("database") != {"port": int(PG_PORT), "name": PG_DB}:
        raise RuntimeError("reviewed database identity mismatch")
    if plan.get("polymarket_mutation") is not False:
        raise RuntimeError("Polymarket boundary missing")
    if plan.get("real_trading_change") is not False:
        raise RuntimeError("real trading boundary missing")
    generated_at = _parse_timestamp(str(plan["generated_at"]))
    age = datetime.now(UTC) - generated_at
    if age < timedelta(minutes=-5) or age > timedelta(hours=MAX_PLAN_AGE_HOURS):
        raise RuntimeError(f"reviewed plan age outside allowed window: {age}")
    cutoff = _parse_timestamp(str(plan["leaderboard"]["cutoff"]))
    policy = plan["policy"]
    if int(policy["historical_leaderboard_bin_hours"]) != HISTORICAL_BIN_HOURS:
        raise RuntimeError("reviewed bin policy mismatch")
    required_true = (
        "keep_all_rows_after_cutoff",
        "all_discarded_snapshot_times_require_raw_provenance",
        "leaderboard_raw_json_reconstructable_from_raw_api_payloads",
        "raw_api_observations_preserved",
        "raw_api_payloads_content_addressed",
        "source_hash_identity_must_be_unique",
        "content_sha256_is_canonical_payload_identity",
        "fills_untouched",
        "raw_api_first_for_headroom",
    )
    for key in required_true:
        if policy.get(key) is not True:
            raise RuntimeError(f"required reviewed policy is not enabled: {key}")
    if policy.get("unnecessary_observation_hash_index") is not False:
        raise RuntimeError("observation hash index must remain disabled")
    if int(plan["leaderboard"]["missing_retained_provenance"]) != 0:
        raise RuntimeError("reviewed retained leaderboard provenance is incomplete")
    if int(plan["leaderboard"]["missing_discarded_provenance"]) != 0:
        raise RuntimeError("reviewed discarded leaderboard provenance is incomplete")
    if int(plan["raw_api"]["source_payload_conflicts"]) != 0:
        raise RuntimeError("reviewed raw API source hash identity is inconsistent")
    if int(plan["raw_api"]["existing_payload_conflicts"]) != 0:
        raise RuntimeError("reviewed existing raw API payloads are inconsistent")
    plan["_cutoff_dt"] = cutoff
    return plan


def _normalize_raw_api_payloads(plan: dict[str, Any]) -> None:
    expected_rows = int(plan["raw_api"]["observation_rows"])
    current_rows = _int("SELECT count(*) FROM raw_api_responses")
    growth = current_rows - expected_rows
    if growth < 0 or growth > MAX_RAW_API_POST_PLAN_ROWS:
        raise RuntimeError(
            f"raw API observation growth outside reviewed bound: {current_rows} vs {expected_rows}"
        )
    required = int(plan["raw_api"]["required_peak_available_bytes"])
    available = _available_bytes()
    if available < required:
        raise RuntimeError(
            "raw API phase headroom fell below reviewed requirement: "
            f"{available} < {required}"
        )

    _psql(
        """
BEGIN;
SET LOCAL lock_timeout='30s';
LOCK TABLE raw_api_responses IN ACCESS EXCLUSIVE MODE;
CREATE TABLE IF NOT EXISTS raw_api_payloads (
    content_sha256 TEXT PRIMARY KEY,
    response_json JSONB NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL
);
DO $verify$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM raw_api_responses
    WHERE response_json <> '{}'::jsonb
    GROUP BY content_sha256
    HAVING count(DISTINCT response_json) > 1
  ) THEN
    RAISE EXCEPTION 'source observations disagree on canonical content hash';
  END IF;
END
$verify$;
DO $verify$
BEGIN
  IF EXISTS (
    SELECT 1 FROM raw_api_responses r JOIN raw_api_payloads p
      ON p.content_sha256=r.content_sha256
    WHERE r.response_json <> '{}'::jsonb
      AND p.response_json IS DISTINCT FROM r.response_json
  ) THEN
    RAISE EXCEPTION 'existing content-addressed payload disagrees with source body';
  END IF;
END
$verify$;
INSERT INTO raw_api_payloads(content_sha256,response_json,first_seen)
SELECT DISTINCT ON(content_sha256) content_sha256,response_json,fetched_at
FROM raw_api_responses
WHERE response_json <> '{}'::jsonb
ORDER BY content_sha256,fetched_at
ON CONFLICT(content_sha256) DO UPDATE
SET first_seen=LEAST(raw_api_payloads.first_seen, EXCLUDED.first_seen);
DO $verify$
BEGIN
  IF EXISTS (
    SELECT 1 FROM raw_api_responses r
    WHERE NOT EXISTS (
      SELECT 1 FROM raw_api_payloads p
      WHERE p.content_sha256=r.content_sha256
    )
  ) THEN
    RAISE EXCEPTION 'raw API payload coverage incomplete';
  END IF;
END
$verify$;
UPDATE raw_api_responses
SET response_json='{}'::jsonb
WHERE response_json <> '{}'::jsonb;
COMMIT;
"""
    )
    _psql("VACUUM (FULL, ANALYZE) raw_api_responses")
    _psql("ANALYZE raw_api_payloads")
    remaining = _int(
        "SELECT count(*) FROM raw_api_responses WHERE response_json <> '{}'::jsonb"
    )
    if remaining:
        raise RuntimeError(
            "raw API writer raced compaction and inserted legacy response bodies: "
            f"{remaining}"
        )


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

    missing_discarded_provenance = _missing_discarded_provenance(cutoff)
    if missing_discarded_provenance:
        raise RuntimeError(
            "discarded leaderboard snapshots lost raw provenance after planning: "
            f"{missing_discarded_provenance}"
        )
    missing_post_cutoff_provenance = _int(
        "SELECT count(*) FROM ("
        " SELECT DISTINCT snapshot_at FROM leaderboard_snapshots"
        f" WHERE snapshot_at > TIMESTAMPTZ '{cutoff}'"
        ") s WHERE NOT EXISTS ("
        " SELECT 1 FROM raw_api_responses r"
        " WHERE r.endpoint='leaderboard' AND r.fetched_at=s.snapshot_at)"
    )
    if missing_post_cutoff_provenance:
        raise RuntimeError(
            f"post-cutoff snapshots missing raw provenance: {missing_post_cutoff_provenance}"
        )
    required = int(plan["leaderboard"]["required_peak_available_bytes"])
    available = _available_bytes()
    if available < required:
        raise RuntimeError(
            f"leaderboard rebuild headroom is insufficient: {available} < {required}"
        )
    expected_keep = int(plan["leaderboard"]["keep_rows_through_cutoff"]) + post_cutoff

    sql = f"""
BEGIN;
SET LOCAL lock_timeout='30s';
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
ALTER TABLE leaderboard_snapshots
    RENAME CONSTRAINT leaderboard_snapshots_compact_v1_address_fkey
    TO leaderboard_snapshots_address_fkey;
ALTER INDEX idx_leaderboard_snapshots_compact_v1_address_time
    RENAME TO idx_leaderboard_snapshots_address_time;
COMMIT;
ANALYZE leaderboard_snapshots;
"""
    _psql(sql)


def apply_plan(path: Path, expected_sha256: str) -> None:
    plan = _validate_plan(path, expected_sha256)
    before_available = _available_bytes()
    if before_available < MIN_AVAILABLE_BYTES:
        raise RuntimeError(f"insufficient preflight headroom: {before_available}")
    subprocess.run(
        [
            "sudo",
            "-n",
            "-u",
            "postgres",
            "/usr/lib/postgresql/14/bin/pg_isready",
            "-p",
            PG_PORT,
        ],
        check=True,
    )
    other_sessions = _active_other_client_sessions()
    if other_sessions:
        raise RuntimeError(
            f"other hlcopy database client sessions are active: {other_sessions}"
        )
    fills_before = _int("SELECT count(*) FROM fills")

    audit: dict[str, Any] = {
        "manifest_sha256": expected_sha256,
        "started_at": datetime.now(UTC).isoformat(),
        "phase": "PRECHECK_COMPLETE",
        "before_available_bytes": before_available,
        "fills_before": fills_before,
        "fills_at_plan": int(plan["fills"]["rows_at_plan"]),
        "leaderboard_compaction_completed": False,
        "raw_api_normalization_completed": False,
        "polymarket_mutation": False,
        "real_trading_change": False,
        "success": False,
    }
    _write_audit(audit)
    try:
        audit["phase"] = "RAW_API_NORMALIZATION_STARTED"
        _write_audit(audit)
        _normalize_raw_api_payloads(plan)
        audit["raw_api_normalization_completed"] = True
        audit["after_raw_api_available_bytes"] = _available_bytes()
        audit["phase"] = "RAW_API_NORMALIZATION_COMPLETE"
        _write_audit(audit)

        other_sessions = _active_other_client_sessions()
        if other_sessions:
            raise RuntimeError(
                "other hlcopy database client sessions appeared before leaderboard phase: "
                f"{other_sessions}"
            )

        audit["phase"] = "LEADERBOARD_COMPACTION_STARTED"
        _write_audit(audit)
        _compact_leaderboard(plan)
        audit["leaderboard_compaction_completed"] = True
        audit["after_leaderboard_available_bytes"] = _available_bytes()
        audit["phase"] = "LEADERBOARD_COMPACTION_COMPLETE"
        _write_audit(audit)

        fills_after = _int("SELECT count(*) FROM fills")
        if fills_after < fills_before:
            raise RuntimeError(
                f"fills decreased during compaction: {fills_before} -> {fills_after}"
            )
        audit.update(
            {
                "fills_after": fills_after,
                "leaderboard_relation_bytes_after": _relation_bytes("leaderboard_snapshots"),
                "raw_api_observations_bytes_after": _relation_bytes("raw_api_responses"),
                "raw_api_payloads_bytes_after": _relation_bytes("raw_api_payloads"),
                "after_available_bytes": _available_bytes(),
                "completed_at": datetime.now(UTC).isoformat(),
                "phase": "COMPLETE",
                "success": True,
            }
        )
        _write_audit(audit)
    except Exception as exc:
        audit.update(
            {
                "failed_at": datetime.now(UTC).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
                "after_available_bytes": _available_bytes(),
                "phase": "FAILED",
                "success": False,
            }
        )
        _write_audit(audit)
        raise
    finally:
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
