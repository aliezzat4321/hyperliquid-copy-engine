from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


PATH = Path(__file__).parents[1] / "scripts" / "storage_exit_gate_report.py"
SPEC = importlib.util.spec_from_file_location("storage_exit_gate_report_test", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_exit_gate_requires_uncontaminated_24_hour_allow_window() -> None:
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
    review = {"reviewed_commit_sha": "commit", "postgres_manifest_sha256": "manifest",
              "reviewer": "CLAUDE_OPUS"}
    result = MODULE.evaluate(
        apply=apply, controller_history={"observations": observations},
        policy=policy, review=review,
    )
    assert result["exit_ready"] is True
    observations[0]["observed_at"] = (completed - timedelta(hours=1)).isoformat()
    result = MODULE.evaluate(
        apply=apply, controller_history={"observations": observations},
        policy=policy, review=review,
    )
    assert result["exit_ready"] is False
    assert result["checks"]["at_least_24_post_reclaim_observations"] is False
