#!/usr/bin/env python3
"""Read-only, fail-closed storage pressure decision controller.

This program emits auditable decisions; deployment wiring owns service control.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC_TZ = timezone(timedelta(0))


def _du(path: Path) -> int:
    if not path.exists():
        raise FileNotFoundError(f"governed dataset path does not exist: {path}")
    return int(subprocess.check_output(["du", "-sb", str(path)], text=True).split()[0])


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def decide(
    policy: dict,
    previous: dict | None,
    *,
    mount: Path,
    now: datetime,
    allow_baseline_without_previous: bool = False,
) -> dict:
    required = {
        "schema_version", "warn_used_pct", "stop_used_pct", "resume_used_pct",
        "minimum_forecast_hours", "history_window_hours", "datasets",
    }
    missing = required - policy.keys()
    if missing:
        raise ValueError(f"policy missing fields: {sorted(missing)}")
    warn, stop, resume = (
        float(policy[key]) for key in ("warn_used_pct", "stop_used_pct", "resume_used_pct")
    )
    if not (0 < warn < resume < stop < 100):
        raise ValueError("policy thresholds must satisfy warn < resume < stop")
    datasets = policy["datasets"]
    if not isinstance(datasets, list) or not datasets:
        raise ValueError("policy must govern at least one dataset")
    maximum_age_hours = float(policy["history_window_hours"])
    if maximum_age_hours <= 0:
        raise ValueError("history_window_hours must be positive")
    if previous is None and not allow_baseline_without_previous:
        raise ValueError(
            "previous observation is required; pass the explicit baseline flag only "
            "when creating the first observation"
        )

    previous_rows = {row["name"]: row for row in (previous or {}).get("datasets", [])}
    previous_at = None
    if previous:
        previous_at = datetime.fromisoformat(
            str(previous["observed_at"]).replace("Z", "+00:00")
        )
        if previous_at.tzinfo is None or now <= previous_at:
            raise ValueError("previous observation time is invalid")
        age_hours = (now - previous_at).total_seconds() / 3600
        if age_hours > maximum_age_hours:
            raise ValueError(
                "previous observation is stale: "
                f"age_hours={age_hours:.3f} history_window_hours={maximum_age_hours}"
            )
    elapsed_hours = (now - previous_at).total_seconds() / 3600 if previous_at else None

    names: set[str] = set()
    rows = []
    dataset_fields = (
        "name", "path", "owner", "writer", "retention_class", "byte_budget",
        "growth_budget_bytes_per_hour", "pressure_control",
    )
    for item in datasets:
        for key in dataset_fields:
            if key not in item or item[key] in (None, ""):
                raise ValueError(f"dataset missing {key}")
        name = str(item["name"])
        relative = Path(str(item["path"]))
        if name in names or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe or duplicate dataset: {name}")
        names.add(name)
        size = _du(mount / relative)
        prior = previous_rows.get(name)
        if previous_at and prior is None:
            raise ValueError(f"previous observation missing governed dataset: {name}")
        rate = (
            None if prior is None or elapsed_hours is None
            else max(0.0, (size - int(prior["bytes"])) / elapsed_hours)
        )
        budget = int(item["byte_budget"])
        growth_budget = int(item["growth_budget_bytes_per_hour"])
        if budget <= 0 or growth_budget <= 0:
            raise ValueError(f"dataset budgets must be positive: {name}")
        rows.append({
            **item,
            "bytes": size,
            "bytes_over_budget": max(0, size - budget),
            "bytes_per_hour": rate,
            "growth_budget_breached": rate is not None and rate > growth_budget,
        })

    usage = shutil.disk_usage(mount)
    used_pct = 100.0 * usage.used / usage.total if usage.total else 100.0
    total_rate = sum(float(row["bytes_per_hour"] or 0) for row in rows)
    hours_to_full = usage.free / total_rate if total_rate > 0 else None
    prior_pressure = bool((previous or {}).get("pressure_active"))
    policy_breach = any(
        row["bytes_over_budget"] or row["growth_budget_breached"] for row in rows
    )
    forecast_breach = (
        hours_to_full is not None
        and hours_to_full < float(policy["minimum_forecast_hours"])
    )
    pressure = (
        used_pct >= stop
        or policy_breach
        or forecast_breach
        or (prior_pressure and used_pct > resume)
    )
    action = "STOP_ALL_MATERIAL_WRITERS" if pressure else (
        "WARN" if used_pct >= warn else "ALLOW"
    )
    return {
        "schema_version": 1,
        "mode": "READ_ONLY_DECISION",
        "observed_at": now.astimezone(UTC_TZ).isoformat(),
        "mount": str(mount),
        "used_bytes": usage.used,
        "available_bytes": usage.free,
        "used_pct": round(used_pct, 3),
        "aggregate_bytes_per_hour": total_rate if previous_at else None,
        "hours_to_full": hours_to_full,
        "pressure_active": pressure,
        "action": action,
        "controlled_writers": sorted({str(row["writer"]) for row in rows}),
        "datasets": rows,
        "real_trading_changed": False,
        "polymarket_mutation": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, default=Path("config/storage_policy.json"))
    parser.add_argument("--previous", type=Path)
    parser.add_argument(
        "--allow-baseline-without-previous",
        action="store_true",
        help="explicitly create the first observation without hysteresis history",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mount", type=Path)
    args = parser.parse_args()
    policy = _load(args.policy)
    mount = args.mount or Path(str(policy["mount"]))
    result = decide(
        policy,
        _load(args.previous) if args.previous else None,
        mount=mount,
        now=datetime.now(UTC_TZ),
        allow_baseline_without_previous=args.allow_baseline_without_previous,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    temporary.replace(args.output)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
