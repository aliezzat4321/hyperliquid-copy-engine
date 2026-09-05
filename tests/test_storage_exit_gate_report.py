from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

PATH = Path(__file__).parents[1] / "scripts" / "storage_exit_gate_report.py"
SPEC = importlib.util.spec_from_file_location("storage_exit_gate_report_test", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

LIFECYCLE_PATH = Path(__file__).parents[1] / "scripts" / "market_tape_lifecycle.py"
LIFECYCLE_SPEC = importlib.util.spec_from_file_location(
    "market_tape_lifecycle_exit_gate_test", LIFECYCLE_PATH
)
assert LIFECYCLE_SPEC and LIFECYCLE_SPEC.loader
LIFECYCLE = importlib.util.module_from_spec(LIFECYCLE_SPEC)
sys.modules[LIFECYCLE_SPEC.name] = LIFECYCLE
LIFECYCLE_SPEC.loader.exec_module(LIFECYCLE)


def test_exit_gate_requires_uncontaminated_24_hour_allow_window(
    tmp_path, monkeypatch,
) -> None:
    completed = datetime(2026, 9, 1, tzinfo=UTC)
    observations = []
    for hour in range(1, 25):
        observations.append({
            "observed_at": (completed + timedelta(hours=hour)).isoformat(),
            "action": "ALLOW", "fail_closed_reason": None,
            "mounts": [{"used_pct": 70, "unaccounted_bytes": 100, "hours_to_full": 72}],
            "datasets": [{"name": "dataset", "bytes_over_budget": 0,
                          "growth_budget_breached": False}],
        })
    apply = {
        "success": True, "phase": "COMPLETE", "completed_at": completed.isoformat(),
        "fills_before": 10, "fills_after": 10, "manifest_sha256": "manifest",
        "leaderboard_relation_bytes_before": 100, "leaderboard_relation_bytes_after": 50,
        "raw_api_observations_bytes_before": 100, "raw_api_observations_bytes_after": 50,
        "before_available_bytes": 100, "after_available_bytes": 200,
        "provenance": {"missing": 0}, "real_trading_change": False,
        "polymarket_mutation": False,
    }
    policy = {"datasets": [{"name": "dataset",
        "owner": "owner", "writer": "writer", "retention_class": "KEEP",
        "byte_budget": 1, "growth_budget_bytes_per_hour": 1,
        "pressure_control": "STOP_WRITER",
    }]}
    tape = tmp_path / "date=2026-08-01" / "coin=BTC" / "channel=trades"
    tape.mkdir(parents=True)
    pl.DataFrame({"coin": ["BTC"], "received_at_ns": [1]}).write_parquet(
        tape / "part.parquet"
    )
    lifecycle_policy = {"policy_version": "LOSSLESS_NORMALIZED_V1", "recent_days": 3,
                        "reader_required_columns": {"trades": ["coin", "received_at_ns"]}}
    lifecycle_path = tmp_path / "lifecycle.json"
    LIFECYCLE.build_plan(
        tmp_path, lifecycle_policy, lifecycle_path, today=date(2026, 9, 3)
    )
    monkeypatch.setattr(
        LIFECYCLE, "disk_usage", lambda _: type("U", (), {"available": 10**9})()
    )
    lifecycle_sha = hashlib.sha256(lifecycle_path.read_bytes()).hexdigest()
    lifecycle = json.loads(json.dumps(LIFECYCLE.apply(
        lifecycle_path, lifecycle_sha, lifecycle_policy, max_age_minutes=30, min_free=0
    )))
    retention = {"retention_apply": "NOT_REQUIRED", "compress_candidates_deleted": 0}
    review = {
        "reviewed_commit_sha": "commit", "postgres_manifest_sha256": "manifest",
        "lifecycle_manifest_sha256": lifecycle_sha, "reviewer": "CLAUDE_OPUS",
    }
    result = MODULE.evaluate(
        apply=apply, controller_history={"observations": observations},
        policy=policy, review=review, lifecycle=lifecycle, retention=retention,
    )
    assert result["exit_ready"] is True

    result = MODULE.evaluate(
        apply=apply, controller_history={"observations": observations},
        policy=policy, review=review,
    )
    assert result["exit_ready"] is False
    assert result["checks"]["lossless_lifecycle"] is False

    lifecycle_without_review = dict(review)
    del lifecycle_without_review["lifecycle_manifest_sha256"]
    result = MODULE.evaluate(
        apply=apply, controller_history={"observations": observations},
        policy=policy, review=lifecycle_without_review, lifecycle=lifecycle,
        retention=retention,
    )
    assert result["exit_ready"] is False
    assert result["checks"]["review_provenance_complete"] is False

    for after_key in (
        "leaderboard_relation_bytes_after", "raw_api_observations_bytes_after",
    ):
        apply_without_after = dict(apply)
        del apply_without_after[after_key]
        result = MODULE.evaluate(
            apply=apply_without_after, controller_history={"observations": observations},
            policy=policy, review=review, lifecycle=lifecycle, retention=retention,
        )
        assert result["exit_ready"] is False
        assert result["checks"]["relations_materially_smaller"] is False

    observations[0]["observed_at"] = (completed - timedelta(hours=1)).isoformat()
    result = MODULE.evaluate(
        apply=apply, controller_history={"observations": observations},
        policy=policy, review=review, lifecycle=lifecycle, retention=retention,
    )
    assert result["exit_ready"] is False
    assert result["checks"]["at_least_24_post_reclaim_observations"] is False
