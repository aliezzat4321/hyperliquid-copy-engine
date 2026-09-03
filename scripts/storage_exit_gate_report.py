#!/usr/bin/env python3
"""Read-only evaluator for every Issue #120 storage exit-gate conjunct."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(
    *, apply: dict[str, Any], controller_history: dict[str, Any],
    policy: dict[str, Any], review: dict[str, Any],
    lifecycle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observations = controller_history.get("observations", [])
    completed = datetime.fromisoformat(str(apply.get("completed_at", "")).replace("Z", "+00:00"))
    post = sorted(
        (x for x in observations if datetime.fromisoformat(
            str(x["observed_at"]).replace("Z", "+00:00")) > completed),
        key=lambda item: item["observed_at"],
    )
    dataset_fields = {
        "owner", "writer", "retention_class", "byte_budget",
        "growth_budget_bytes_per_hour", "pressure_control",
    }
    policy_dataset_names = {str(item.get("name")) for item in policy.get("datasets", [])}
    provenance = apply.get("provenance", {})
    mounts = [mount for observation in post for mount in observation.get("mounts", [])]
    datasets = [dataset for observation in post for dataset in observation.get("datasets", [])]
    checks = {
        "postgres_apply_complete": apply.get("success") is True
        and apply.get("phase") == "COMPLETE",
        "fills_preserved": int(apply.get("fills_after", -1)) >= int(apply.get("fills_before", 0)),
        "provenance_zero": bool(provenance) and all(
            int(value) == 0 for value in provenance.values()
        ),
        "relations_materially_smaller": (
            "leaderboard_relation_bytes_after" in apply
            and "raw_api_observations_bytes_after" in apply
            and int(apply["leaderboard_relation_bytes_after"])
            < int(apply.get("leaderboard_relation_bytes_before", 0))
            and int(apply["raw_api_observations_bytes_after"])
            < int(apply.get("raw_api_observations_bytes_before", 0))
        ),
        "filesystem_reclaimed": int(apply.get("after_available_bytes", 0))
        > int(apply.get("before_available_bytes", 0)),
        "lossless_lifecycle": lifecycle is None or (
            int(lifecycle.get("rows_lost", -1)) == 0
            and lifecycle.get("reader_column_registry_satisfied") is True
            and int(lifecycle.get("compress_candidates_deleted", -1)) == 0
        ),
        "at_least_24_post_reclaim_observations": len(post) >= 24 and (
            datetime.fromisoformat(str(post[-1]["observed_at"]).replace("Z", "+00:00"))
            - datetime.fromisoformat(str(post[0]["observed_at"]).replace("Z", "+00:00"))
        ) >= timedelta(hours=23),
        "all_post_reclaim_allow": len(post) >= 24 and all(
            x.get("action") == "ALLOW" and x.get("fail_closed_reason") is None for x in post
        ),
        "post_reclaim_bounds": bool(post) and all(
            float(mount.get("used_pct", 100)) < 75
            and float(mount.get("unaccounted_bytes", 2**63)) <= 4 * 1024**3
            and mount.get("hours_to_full") is not None
            and float(mount["hours_to_full"]) >= 48
            for mount in mounts
        ) and all(
            int(dataset.get("bytes_over_budget", 1)) == 0
            and dataset.get("growth_budget_breached") is False
            for dataset in datasets
        ),
        "policy_complete": bool(policy.get("datasets")) and all(
            dataset_fields <= item.keys()
            and all(item.get(field) not in (None, "") for field in dataset_fields)
            for item in policy.get("datasets", [])
        ) and all(
            policy_dataset_names
            == {str(item.get("name")) for item in observation.get("datasets", [])}
            for observation in post
        ),
        "review_provenance_complete": all(review.get(field) for field in (
            "reviewed_commit_sha", "postgres_manifest_sha256", "reviewer",
        )) and review.get("postgres_manifest_sha256") == apply.get("manifest_sha256")
        and (
            lifecycle is None
            or review.get("lifecycle_manifest_sha256")
            == lifecycle.get("manifest_sha256")
        ),
        "safety_boundaries": apply.get("real_trading_change") is False
        and apply.get("polymarket_mutation") is False,
    }
    return {
        "schema_version": 1,
        "mode": "READ_ONLY_EXIT_GATE",
        "evaluated_at": datetime.now(UTC).isoformat(),
        "exit_ready": all(checks.values()),
        "checks": checks,
        "post_reclaim_observations": len(post),
        "actual_gib_reclaimed": round(
            (
                int(apply.get("after_available_bytes", 0))
                - int(apply.get("before_available_bytes", 0))
            )
            / 1024**3, 3
        ),
        "real_trading_change": False,
        "polymarket_mutation": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-audit", type=Path, required=True)
    parser.add_argument("--controller-history", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=Path("config/storage_policy.json"))
    parser.add_argument("--review-provenance", type=Path, required=True)
    parser.add_argument("--lifecycle-audit", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(
        apply=_load(args.apply_audit), controller_history=_load(args.controller_history),
        policy=_load(args.policy), review=_load(args.review_provenance),
        lifecycle=_load(args.lifecycle_audit) if args.lifecycle_audit else None,
    )
    result["input_sha256"] = {
        "apply_audit": _sha(args.apply_audit),
        "controller_history": _sha(args.controller_history),
        "policy": _sha(args.policy),
        "review_provenance": _sha(args.review_provenance),
        **({"lifecycle_audit": _sha(args.lifecycle_audit)} if args.lifecycle_audit else {}),
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if result["exit_ready"] else 2)


if __name__ == "__main__":
    main()
