#!/usr/bin/env python3
"""Read-only, fail-closed storage pressure decision controller (policy v2)."""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hlcopy.storage.metrics import disk_usage

UTC_TZ = UTC
CONTROLS = {"STOP_WRITER", "THROTTLE", "NEVER_STOP"}


def _du(path: Path) -> int:
    if not path.exists():
        raise FileNotFoundError(f"governed dataset path does not exist: {path}")
    return int(subprocess.check_output(["du", "-sb", str(path)], text=True).split()[0])


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _at(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("observation timestamp must be timezone-aware")
    return result.astimezone(UTC_TZ)


def _median_rate(samples: list[tuple[datetime, int]]) -> float | None:
    rates = [(b - a) / ((bt - at).total_seconds() / 3600)
             for i, (at, a) in enumerate(samples) for bt, b in samples[i + 1:] if bt > at]
    return max(0.0, statistics.median(rates)) if len(samples) >= 3 and rates else None


def _validate(policy: dict[str, Any], usages: dict[str, Any]) -> None:
    if policy.get("schema_version") != 2:
        raise ValueError("storage policy schema_version must be 2")
    mounts, datasets = policy.get("mounts"), policy.get("datasets")
    if not isinstance(mounts, list) or not mounts or not isinstance(datasets, list) or not datasets:
        raise ValueError("policy must contain non-empty mounts and datasets")
    mount_names: set[str] = set()
    mount_fields = {"name", "path", "warn_used_pct", "resume_used_pct", "stop_used_pct",
                    "target_used_pct", "minimum_forecast_hours", "history_window_hours",
                    "unallocated_reserve_bytes", "unaccounted_budget_bytes"}
    for item in mounts:
        if missing := mount_fields - item.keys():
            raise ValueError(f"mount missing fields: {sorted(missing)}")
        name = str(item["name"])
        if name in mount_names:
            raise ValueError(f"duplicate mount: {name}")
        mount_names.add(name)
        warn, resume, stop = (
            float(item[k])
            for k in ("warn_used_pct", "resume_used_pct", "stop_used_pct")
        )
        target = float(item["target_used_pct"])
        if not (0 < warn < resume < stop < 100) or not (0 < target < 80):
            raise ValueError(f"invalid thresholds for mount: {name}")
    names, sums = set(), {name: 0 for name in mount_names}
    fields = {"name", "mount", "path", "owner", "writer", "retention_class", "byte_budget",
              "steady_state_bytes", "growth_budget_bytes_per_hour", "pressure_control",
              "retention_horizon_hours", "reclaim_actuator"}
    for item in datasets:
        if missing := fields - item.keys():
            raise ValueError(f"dataset missing fields: {sorted(missing)}")
        name = str(item["name"])
        mount = str(item["mount"])
        rel = Path(str(item["path"]))
        if name in names or mount not in mount_names or rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"unsafe or duplicate dataset: {name}")
        names.add(name)
        if item["pressure_control"] not in CONTROLS:
            raise ValueError(f"invalid pressure_control: {name}")
        budget, steady, growth, horizon = (
            int(item[k])
            for k in (
                "byte_budget",
                "steady_state_bytes",
                "growth_budget_bytes_per_hour",
                "retention_horizon_hours",
            )
        )
        if min(budget, growth, horizon) <= 0 or not 0 <= steady <= budget:
            raise ValueError(f"invalid dataset budget: {name}")
        if growth > (budget - steady) / horizon:
            raise ValueError(f"growth budget cannot bind before byte budget: {name}")
        sums[mount] += budget
    for item in mounts:
        name = str(item["name"])
        allocation = sums[name] + int(item["unallocated_reserve_bytes"])
        if allocation > usages[name].capacity_df * float(item["target_used_pct"]) / 100:
            raise ValueError(f"dataset budgets plus reserve exceed target capacity: {name}")


def decide(
    policy: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    now: datetime,
    allow_baseline_without_previous: bool = False,
) -> dict[str, Any]:
    mount_defs = policy.get("mounts") or []
    usages = {str(x["name"]): disk_usage(Path(str(x["path"]))) for x in mount_defs}
    _validate(policy, usages)
    if not history and not allow_baseline_without_previous:
        raise ValueError(
            "previous observation is required; use the explicit baseline flag once"
        )
    latest = history[-1] if history else None
    configs = {str(x["name"]): x for x in mount_defs}
    maximum_window = max(float(x["history_window_hours"]) for x in mount_defs)
    if latest:
        age = (now - _at(str(latest["observed_at"]))).total_seconds() / 3600
        if age <= 0 or age > maximum_window:
            raise ValueError("previous observation is stale or from the future")
        history = [
            observation for observation in history
            if 0 < (now - _at(str(observation["observed_at"]))).total_seconds() / 3600
            <= maximum_window
        ]
    prior = {x["name"]: x for x in (latest or {}).get("datasets", [])}
    rows = []
    for item in policy["datasets"]:
        name, mount = str(item["name"]), str(item["mount"])
        size = _du(Path(str(configs[mount]["path"])) / str(item["path"]))
        samples = []
        for observation in history:
            indexed = {x["name"]: x for x in observation.get("datasets", [])}
            if name in indexed:
                samples.append(
                    (_at(str(observation["observed_at"])), int(indexed[name]["bytes"]))
                )
        samples.append((now, size))
        instantaneous = None
        if name in prior and latest:
            elapsed = (now - _at(str(latest["observed_at"]))).total_seconds() / 3600
            instantaneous = max(0.0, (size - int(prior[name]["bytes"])) / elapsed)
        rate, budget = _median_rate(samples), int(item["byte_budget"])
        rows.append({**item, "bytes": size, "bytes_over_budget": max(0, size - budget),
                     "instantaneous_bytes_per_hour": instantaneous, "bytes_per_hour": rate,
                     "growth_budget_breached": rate is not None and rate > int(
                         item["growth_budget_bytes_per_hour"]),
                     "hours_to_budget": (
                         (budget - size) / rate if rate and size < budget
                         else (0.0 if size >= budget else None)
                     )})
    mounts, pressure, prior_pressure = [], False, bool((latest or {}).get("pressure_active"))
    for name, usage in usages.items():
        cfg = configs[name]
        governed = sum(x["bytes"] for x in rows if x["mount"] == name)
        unaccounted = max(0, usage.used - governed)
        rates = [
            x["bytes_per_hour"]
            for x in rows
            if x["mount"] == name and x["bytes_per_hour"] is not None
        ]
        hours_to_full = usage.available / sum(rates) if sum(rates) > 0 else None
        breach = unaccounted > int(cfg["unaccounted_budget_bytes"])
        active = (usage.used_pct >= float(cfg["stop_used_pct"]) or breach or
                  any(
                      x["bytes_over_budget"] or x["growth_budget_breached"]
                      for x in rows if x["mount"] == name
                  ) or
                  (hours_to_full is not None and
                   hours_to_full < float(cfg["minimum_forecast_hours"])) or
                  (prior_pressure and usage.used_pct > float(cfg["resume_used_pct"])))
        pressure |= active
        mounts.append({"name": name, "path": cfg["path"], "capacity_df_bytes": usage.capacity_df,
                       "used_bytes": usage.used, "available_bytes": usage.available,
                       "used_pct": round(usage.used_pct, 3), "unaccounted_bytes": unaccounted,
                       "unaccounted_budget_breached": breach, "hours_to_full": hours_to_full})
    actions = [{"writer": x["writer"], "action": x["pressure_control"] if pressure else "ALLOW"}
               for x in rows if x["pressure_control"] != "NEVER_STOP"]
    warn = any(
        x["used_pct"] >= float(configs[x["name"]]["warn_used_pct"])
        for x in mounts
    )
    action = "STOP_ALL_MATERIAL_WRITERS" if pressure else ("WARN" if warn else "ALLOW")
    return {"schema_version": 2, "mode": "READ_ONLY_DECISION",
            "observed_at": now.astimezone(UTC_TZ).isoformat(),
            "pressure_active": pressure, "action": action,
            "writer_actions": actions,
            "controlled_writers": sorted({x["writer"] for x in actions}),
            "mounts": mounts, "datasets": rows, "fail_closed_reason": None,
            "real_trading_changed": False, "polymarket_mutation": False}


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _fail_closed_actions(policy_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """Best-effort enumeration; an invalid policy must never yield an empty stop set."""
    try:
        datasets = _load(policy_path).get("datasets", [])
        writers = sorted(
            {str(item.get("writer", "")).strip() for item in datasets}
            - {""}
        )
    except Exception:
        writers = []
    return ([{"writer": writer, "action": "STOP_WRITER"} for writer in writers], writers)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, default=Path("config/storage_policy.json"))
    parser.add_argument("--history", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--allow-baseline-without-previous", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        policy = _load(args.policy)
        history = (
            _load(args.history).get("observations", [])
            if args.history else ([_load(args.previous)] if args.previous else [])
        )
        result = decide(
            policy,
            history,
            now=datetime.now(UTC_TZ),
            allow_baseline_without_previous=args.allow_baseline_without_previous,
        )
    except Exception as exc:
        writer_actions, controlled_writers = _fail_closed_actions(args.policy)
        result = {"schema_version": 2, "mode": "READ_ONLY_DECISION",
                  "observed_at": datetime.now(UTC_TZ).isoformat(),
                  "pressure_active": True, "action": "STOP_ALL_MATERIAL_WRITERS",
                  "controlled_writers": controlled_writers,
                  "writer_actions": writer_actions,
                  "fail_closed_reason": f"{type(exc).__name__}: {exc}",
                  "real_trading_changed": False, "polymarket_mutation": False}
        _write(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(2) from exc
    _write(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
