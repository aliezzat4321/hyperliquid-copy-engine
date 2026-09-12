#!/usr/bin/env python3
"""Fail-closed verification of trusted Opus storage-review provenance for #90."""

from __future__ import annotations

import argparse
import json
import sqlite3
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

EXPECTED_ISSUE = 90
EXPECTED_PR = 288
EXPECTED_AGENT = "CLAUDE"
EXPECTED_MODEL = "OPUS"
EXPECTED_TASK_TYPES = {"REVIEW", "DESTRUCTIVE_REVIEW"}
EXPECTED_STATUS = "DONE"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _assert_root_owned(path: Path) -> None:
    info = path.stat()
    if info.st_uid != 0:
        raise ValueError(f"trusted runtime evidence is not root-owned: {path}")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(f"trusted runtime evidence is group/world writable: {path}")


def _parse_markers(text: str) -> dict[str, str]:
    markers: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().strip("`*")
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in markers and markers[key] != value:
            raise ValueError(f"conflicting review marker: {key}")
        markers[key] = value
    return markers


def verify(
    *,
    db_path: Path,
    runtime_root: Path,
    assignment_id: str,
    code_sha: str,
    plan_run_id: str,
    plan_completed_at: str,
    retention_sha: str,
    bootstrap_sha: str,
    lifecycle_sha: str,
    binding_sha: str,
    require_root_owner: bool = True,
) -> dict[str, Any]:
    if require_root_owner:
        _assert_root_owned(db_path)

    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        task = db.execute("SELECT * FROM tasks WHERE id=?", (assignment_id,)).fetchone()
        if task is None:
            raise ValueError("trusted Opus assignment not found in root-owned ledger")
        task = dict(task)
        expected_task = {
            "issue_number": EXPECTED_ISSUE,
            "pr_number": EXPECTED_PR,
            "agent": EXPECTED_AGENT,
            "model_class": EXPECTED_MODEL,
            "status": EXPECTED_STATUS,
            "target_sha": code_sha,
        }
        for key, expected in expected_task.items():
            if task.get(key) != expected:
                raise ValueError(f"trusted assignment mismatch: {key}")
        if task.get("task_type") not in EXPECTED_TASK_TYPES:
            raise ValueError("trusted assignment is not an Opus review task")

        run = db.execute(
            "SELECT * FROM runs WHERE task_id=? AND exit_code=0 AND ended_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (assignment_id,),
        ).fetchone()
        if run is None:
            raise ValueError("trusted Opus assignment has no successful completed run")
        run = dict(run)

    if _parse_time(str(run["ended_at"])) < _parse_time(plan_completed_at):
        raise ValueError("trusted Opus run predates completed immutable plan")

    run_dir = runtime_root / "runs" / str(run["id"])
    meta_path = run_dir / "meta.json"
    result_path = run_dir / "result.json"
    if not meta_path.is_file() or not result_path.is_file():
        raise ValueError("trusted runtime run files are missing")
    if require_root_owner:
        _assert_root_owned(run_dir)
        _assert_root_owned(meta_path)
        _assert_root_owned(result_path)

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    result_file = json.loads(result_path.read_text(encoding="utf-8"))
    expected_meta = {
        "assignment_id": assignment_id,
        "agent": EXPECTED_AGENT,
        "model": EXPECTED_MODEL,
        "issue": EXPECTED_ISSUE,
        "pr": EXPECTED_PR,
        "target_sha": code_sha,
        "status": EXPECTED_STATUS,
    }
    for key, expected in expected_meta.items():
        if meta.get(key) != expected:
            raise ValueError(f"trusted run metadata mismatch: {key}")
    result_metadata_matches = (
        result_file.get("run_id") == run["id"]
        and result_file.get("status") == EXPECTED_STATUS
    )
    if not result_metadata_matches:
        raise ValueError("trusted run result metadata mismatch")

    db_result = str(run.get("result") or "")
    file_result = str(result_file.get("result") or "")
    if not db_result or db_result != file_result:
        raise ValueError("trusted run result is missing or inconsistent")

    markers = _parse_markers(db_result)
    required = {
        "SECOND_PASS_GATE": "PASS",
        "MODEL_CLASS": EXPECTED_MODEL,
        "ASSIGNMENT_ID": assignment_id,
        "TARGET_SHA": code_sha,
        "PLAN_RUN_ID": plan_run_id,
        "RETENTION_MANIFEST_SHA256": retention_sha,
        "BOOTSTRAP_PLAN_SHA256": bootstrap_sha,
        "LIFECYCLE_MANIFEST_SHA256": lifecycle_sha,
        "REVIEW_BINDING_SHA256": binding_sha,
        "DESTRUCTIVE_STORAGE_APPLY": "APPROVED",
        "REAL_TRADING_ENABLED": "NO",
        "POSTGRESQL_FILESYSTEM_DELETION": "NO",
        "POLYMARKET_MUTATION": "NO",
    }
    for key, expected in required.items():
        if markers.get(key) != expected:
            raise ValueError(f"trusted Opus result missing/mismatched marker: {key}")

    return {
        "assignment_id": assignment_id,
        "run_id": run["id"],
        "model": EXPECTED_MODEL,
        "target_sha": code_sha,
        "verdict": "PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("/var/lib/hyperliquid-ai-team/orchestrator/ledger.sqlite3"),
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path("/var/lib/hyperliquid-ai-team"),
    )
    parser.add_argument("--assignment-id", required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--plan-run-id", required=True)
    parser.add_argument("--plan-completed-at", required=True)
    parser.add_argument("--retention-sha", required=True)
    parser.add_argument("--bootstrap-sha", required=True)
    parser.add_argument("--lifecycle-sha", required=True)
    parser.add_argument("--binding-sha", required=True)
    args = parser.parse_args()
    proof = verify(
        db_path=args.db,
        runtime_root=args.runtime_root,
        assignment_id=args.assignment_id,
        code_sha=args.code_sha,
        plan_run_id=args.plan_run_id,
        plan_completed_at=args.plan_completed_at,
        retention_sha=args.retention_sha,
        bootstrap_sha=args.bootstrap_sha,
        lifecycle_sha=args.lifecycle_sha,
        binding_sha=args.binding_sha,
    )
    print("TRUSTED_OPUS_ASSIGNMENT=PASS")
    print(f"TRUSTED_OPUS_RUN_ID={proof['run_id']}")
    print(f"TRUSTED_OPUS_TARGET_SHA={proof['target_sha']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
