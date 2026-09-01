#!/usr/bin/env python3
"""Fail-closed Hyperliquid storage pressure controller.

The controller only manages the writer units declared in the reviewed policy.  It
never performs retention or database compaction; those remain separate,
manifest-bound operations.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def validate_policy(policy: dict[str, Any]) -> list[str]:
    if policy.get("schema_version") != 1:
        raise ValueError("unsupported storage policy schema")
    thresholds = policy.get("policy")
    datasets = policy.get("datasets")
    if not isinstance(thresholds, dict) or not isinstance(datasets, list) or not datasets:
        raise ValueError("policy and non-empty datasets are required")
    healthy = float(thresholds["healthy_used_pct"])
    warn = float(thresholds["warn_used_pct"])
    resume = float(thresholds["resume_used_pct"])
    stop = float(thresholds["stop_used_pct"])
    if not (0 < healthy < resume < warn < stop < 100):
        raise ValueError("thresholds must satisfy healthy < resume < warn < stop")
    if float(thresholds["minimum_time_to_full_hours"]) <= 0:
        raise ValueError("minimum_time_to_full_hours must be positive")
    names: set[str] = set()
    writers: list[str] = []
    for row in datasets:
        if not isinstance(row, dict):
            raise ValueError("dataset entries must be objects")
        required = (
            "name",
            "owner",
            "retention_class",
            "byte_budget",
            "growth_budget_bytes_per_hour",
        )
        if any(not row.get(key) for key in required):
            raise ValueError(
                f"dataset is missing ownership/retention/budget fields: {row!r}"
            )
        name = str(row["name"])
        if name in names:
            raise ValueError(f"duplicate dataset: {name}")
        names.add(name)
        if int(row["byte_budget"]) <= 0 or int(row["growth_budget_bytes_per_hour"]) <= 0:
            raise ValueError(f"dataset budgets must be positive: {name}")
        units = row.get("writers")
        if not isinstance(units, list) or not units:
            raise ValueError(f"dataset has no governed writer: {name}")
        for unit in units:
            unit = str(unit)
            if not unit.startswith("hyperliquid-") or not unit.endswith((".service", ".timer")):
                raise ValueError(f"unsafe writer unit: {unit}")
            writers.append(unit)
    return sorted(set(writers))


def evaluate(
    policy: dict[str, Any],
    *,
    total_bytes: int,
    used_bytes: int,
    previous: dict[str, Any] | None,
    now: datetime,
    paused: bool,
) -> dict[str, Any]:
    writers = validate_policy(policy)
    if total_bytes <= 0 or used_bytes < 0 or used_bytes > total_bytes:
        raise ValueError("invalid filesystem byte observation")
    cfg = policy["policy"]
    used_pct = used_bytes / total_bytes * 100
    growth_bph: float | None = None
    if previous:
        prior_at = datetime.fromisoformat(str(previous["observed_at"]).replace("Z", "+00:00"))
        elapsed = (now - prior_at.astimezone(timezone.utc)).total_seconds() / 3600
        if elapsed <= 0:
            raise ValueError("previous observation is not older than current observation")
        growth_bph = (used_bytes - int(previous["used_bytes"])) / elapsed
    available = total_bytes - used_bytes
    time_to_full = available / growth_bph if growth_bph is not None and growth_bph > 0 else None
    budget_bph = sum(int(row["growth_budget_bytes_per_hour"]) for row in policy["datasets"])
    forecast_breach = time_to_full is not None and time_to_full < float(
        cfg["minimum_time_to_full_hours"]
    )
    growth_breach = growth_bph is not None and growth_bph > budget_bph
    pressure = used_pct >= float(cfg["stop_used_pct"]) or forecast_breach or growth_breach
    resume_safe = (
        used_pct <= float(cfg["resume_used_pct"])
        and not forecast_breach
        and not growth_breach
    )
    desired_paused = pressure or (paused and not resume_safe)
    if desired_paused:
        state = "PAUSE" if pressure else "HOLD_PAUSED"
    elif paused:
        state = "RESUME"
    elif used_pct >= float(cfg["warn_used_pct"]):
        state = "WARN"
    else:
        state = "OK"
    return {
        "schema_version": 1,
        "observed_at": now.isoformat(),
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "available_bytes": available,
        "used_pct": round(used_pct, 3),
        "growth_bytes_per_hour": None if growth_bph is None else round(growth_bph, 3),
        "aggregate_growth_budget_bytes_per_hour": budget_bph,
        "time_to_full_hours": None if time_to_full is None else round(time_to_full, 3),
        "forecast_breach": forecast_breach,
        "growth_budget_breach": growth_breach,
        "state": state,
        "writers_paused": desired_paused,
        "governed_writers": writers,
        "real_trading_changed": False,
        "retention_applied": False,
    }


def _set_units(units: list[str], action: str) -> None:
    for unit in units:
        subprocess.run(["systemctl", action, unit], check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--policy", type=Path, default=Path("config/storage_governance_v1.json")
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--apply-pressure", action="store_true")
    args = parser.parse_args()
    policy = _read_object(args.policy)
    writers = validate_policy(policy)
    prior = _read_object(args.state) if args.state.exists() else None
    paused = bool(prior and prior.get("writers_paused"))
    usage = shutil.disk_usage(str(policy["mount"]))
    result = evaluate(
        policy,
        total_bytes=usage.total,
        used_bytes=usage.used,
        previous=prior,
        now=datetime.now(timezone.utc),
        paused=paused,
    )
    if args.apply_pressure and result["state"] in {"PAUSE", "RESUME"}:
        _set_units(writers, "stop" if result["state"] == "PAUSE" else "start")
    args.state.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.state.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.state)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
