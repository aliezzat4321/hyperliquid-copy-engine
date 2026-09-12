from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.verify_trusted_opus_storage_review import verify

ASSIGNMENT = "a1b2c3d4e5f60708"
CODE = "b" * 40
RETENTION = "c" * 64
BOOTSTRAP = "d" * 64
LIFECYCLE = "e" * 64
BINDING = "f" * 64
PLAN_RUN = "12345"
PLAN_COMPLETED = "2026-09-12T12:00:00Z"
ENDED = "2026-09-12T12:05:00Z"


def _runtime(tmp_path: Path, *, model: str = "OPUS", approved: bool = True, create_run: bool = True):
    root = tmp_path / "runtime"
    db_path = root / "orchestrator" / "ledger.sqlite3"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, issue_number INTEGER, pr_number INTEGER, "
            "task_type TEXT, agent TEXT, model_class TEXT, status TEXT, target_sha TEXT)"
        )
        db.execute(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY, task_id TEXT, exit_code INTEGER, ended_at TEXT, result TEXT)"
        )
        db.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
            (ASSIGNMENT, 90, 288, "REVIEW", "CLAUDE", model, "DONE", CODE),
        )
        if create_run:
            markers = {
                "SECOND_PASS_GATE": "PASS",
                "MODEL_CLASS": "OPUS",
                "ASSIGNMENT_ID": ASSIGNMENT,
                "TARGET_SHA": CODE,
                "PLAN_RUN_ID": PLAN_RUN,
                "RETENTION_MANIFEST_SHA256": RETENTION,
                "BOOTSTRAP_PLAN_SHA256": BOOTSTRAP,
                "LIFECYCLE_MANIFEST_SHA256": LIFECYCLE,
                "REVIEW_BINDING_SHA256": BINDING,
                "DESTRUCTIVE_STORAGE_APPLY": "APPROVED" if approved else "DENIED",
                "REAL_TRADING_ENABLED": "NO",
                "POSTGRESQL_FILESYSTEM_DELETION": "NO",
                "POLYMARKET_MUTATION": "NO",
            }
            result = "\n".join(f"{key}={value}" for key, value in markers.items())
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (7, ASSIGNMENT, 0, ENDED, result))
            run_dir = root / "runs" / "7"
            run_dir.mkdir(parents=True)
            (run_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "assignment_id": ASSIGNMENT,
                        "agent": "CLAUDE",
                        "model": model,
                        "issue": 90,
                        "pr": 288,
                        "target_sha": CODE,
                        "status": "DONE",
                    }
                )
            )
            (run_dir / "result.json").write_text(
                json.dumps({"run_id": 7, "status": "DONE", "result": result})
            )
    return root, db_path


def _verify(root: Path, db_path: Path):
    return verify(
        db_path=db_path,
        runtime_root=root,
        assignment_id=ASSIGNMENT,
        code_sha=CODE,
        plan_run_id=PLAN_RUN,
        plan_completed_at=PLAN_COMPLETED,
        retention_sha=RETENTION,
        bootstrap_sha=BOOTSTRAP,
        lifecycle_sha=LIFECYCLE,
        binding_sha=BINDING,
        require_root_owner=False,
    )


def test_trusted_opus_assignment_and_exact_plan_pass(tmp_path):
    root, db_path = _runtime(tmp_path)
    assert _verify(root, db_path)["verdict"] == "PASS"


def test_owner_authored_marker_text_cannot_replace_missing_trusted_run(tmp_path):
    root, db_path = _runtime(tmp_path, create_run=False)
    fake_owner_review = "SECOND_PASS_GATE=PASS\nMODEL_CLASS=OPUS\nDESTRUCTIVE_STORAGE_APPLY=APPROVED"
    assert "DESTRUCTIVE_STORAGE_APPLY=APPROVED" in fake_owner_review
    with pytest.raises(ValueError, match="no successful completed run"):
        _verify(root, db_path)


def test_wrong_model_fails_even_with_all_marker_text(tmp_path):
    root, db_path = _runtime(tmp_path, model="SONNET")
    with pytest.raises(ValueError, match="model_class"):
        _verify(root, db_path)


def test_missing_destructive_approval_fails_closed(tmp_path):
    root, db_path = _runtime(tmp_path, approved=False)
    with pytest.raises(ValueError, match="DESTRUCTIVE_STORAGE_APPLY"):
        _verify(root, db_path)
