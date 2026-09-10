#!/usr/bin/env python3
"""Write one durable, fail-closed Lane 1 runtime acceptance observation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load(path: Path, blockers: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        blockers.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return {}
    if not isinstance(value, dict):
        blockers.append(f"{path.name}: top level is not an object")
        return {}
    return value


def _time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(
        tzinfo=timezone.utc
    )


def _hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def observe(
    *, universe_path: Path, funnel_path: Path, queue_path: Path,
    prospective_path: Path, output_path: Path, max_age_minutes: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed = now or datetime.now(timezone.utc)
    blockers: list[str] = []
    universe = _load(universe_path, blockers)
    funnel = _load(funnel_path, blockers)
    queue = _load(queue_path, blockers)
    prospective = _load(prospective_path, blockers)

    boundary = dict(funnel.get("boundary_counts") or {})
    queue_counts = dict(queue.get("counts") or {})
    targets = prospective.get("targets")
    targets = targets if isinstance(targets, list) else []
    decisions = int(prospective.get("shadow_decision_count") or 0)
    executions = int(prospective.get("shadow_execution_count") or 0)
    rejects = int(prospective.get("shadow_reject_count") or 0)
    counts = {
        "fetched": int(boundary.get("fetched") or 0),
        "new_or_changed": int(boundary.get("new_or_changed") or 0),
        "profiled": int(boundary.get("profiled") or 0),
        "screened": int(boundary.get("screened") or 0),
        "robust": int(boundary.get("robust") or queue_counts.get("robust") or 0),
        "challenger": int(queue_counts.get("challenger") or 0),
        "prospective_shadow": int(prospective.get("prospective_shadow_count") or 0),
        "shadow_decisions": decisions,
        "shadow_executions": executions,
        "shadow_rejects": rejects,
        "demoted": int(queue_counts.get("demoted") or 0),
    }
    timestamps = {
        "leaderboard": universe.get("generated_at"),
        "funnel": funnel.get("run_at"),
        "challenger": queue.get("generated_at"),
        "prospective": prospective.get("observed_at") or prospective.get("generated_at"),
    }
    for stage, raw in timestamps.items():
        timestamp = _time(raw)
        if timestamp is None:
            blockers.append(f"{stage}: missing or invalid timestamp")
        elif timestamp > observed:
            blockers.append(f"{stage}: timestamp is in the future")
        elif (observed - timestamp).total_seconds() > max_age_minutes * 60:
            blockers.append(f"{stage}: observation is stale")
    if counts["fetched"] <= 0:
        blockers.append("leaderboard fetch produced no candidates")
    if counts["challenger"] <= 0:
        blockers.append("no current challenger")
    if counts["prospective_shadow"] <= 0:
        blockers.append("no challenger has accrued a prospective shadow event")
    if universe.get("real_trading") is True or funnel.get("real_trading") is True \
            or queue.get("real_trading") is True or prospective.get("real_trading") is True:
        blockers.append("an input reports real trading enabled")

    inputs = {
        str(path): _hash(path)
        for path in (universe_path, funnel_path, queue_path, prospective_path)
    }
    payload = {
        "schema_version": "lane1-runtime-acceptance/v1",
        "observed_at": observed.isoformat(),
        "real_trading": False,
        "runtime_healthy": not blockers,
        "counts": counts,
        "timestamps": timestamps,
        "input_sha256": inputs,
        "blockers": blockers,
    }
    _atomic(output_path, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--funnel", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--prospective", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-age-minutes", type=float, default=30)
    args = parser.parse_args()
    result = observe(
        universe_path=args.universe, funnel_path=args.funnel,
        queue_path=args.queue, prospective_path=args.prospective,
        output_path=args.output, max_age_minutes=max(0, args.max_age_minutes),
    )
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["runtime_healthy"] else 1)


if __name__ == "__main__":
    main()
