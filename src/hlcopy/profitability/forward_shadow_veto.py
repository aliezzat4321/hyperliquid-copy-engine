from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

D = Decimal
SAFETY_BLOCKERS = {"INCOMPLETE_PATH_TRUTH", "NO_SAFE_LEVERAGE_ACROSS_SCENARIOS"}
VETO_BLOCKER = "FORWARD_EMERGENCY_VETO_ACTIVE"


@dataclass(frozen=True, slots=True)
class ForwardVetoConfig:
    min_actions: int = 30
    min_mature_notionals: int = 3
    hard_negative_bps: Decimal = D("-25")
    persistent_negative_cycles: int = 2
    recovery_return_bps: Decimal = D("10")
    recovery_cycles: int = 2
    recovery_age_hours: Decimal = D("24")


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "mode": "PROSPECTIVE_FORWARD_SHADOW_VETO_V1",
            "real_trading": False,
            "wallet_states": {},
            "veto_intervals": [],
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("real_trading") is not False:
        raise ValueError("forward veto store must be research-only")
    return data


def _wallet_rows(path_truth: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for raw in path_truth.get("promotion_candidates") or []:
        if not isinstance(raw, dict):
            continue
        wallet = str(raw.get("wallet_address") or "").lower()
        if wallet:
            out.setdefault(wallet, []).append(raw)
    return out


def _close_open_interval(intervals: list[dict[str, Any]], wallet: str, now_ns: int) -> None:
    for row in reversed(intervals):
        if (
            str(row.get("wallet_address") or "").lower() == wallet
            and row.get("effective_until_ns") is None
        ):
            row["effective_until_ns"] = now_ns
            return


def evaluate_forward_vetoes(
    *,
    path_truth: dict[str, Any],
    existing: dict[str, Any],
    now_ns: int,
    config: ForwardVetoConfig | None = None,
) -> dict[str, Any]:
    config = config or ForwardVetoConfig()
    states = dict(existing.get("wallet_states") or {})
    intervals = [dict(row) for row in existing.get("veto_intervals") or [] if isinstance(row, dict)]
    rows_by_wallet = _wallet_rows(path_truth)

    for wallet, rows in rows_by_wallet.items():
        previous = dict(states.get(wallet) or {})
        active_before = bool(previous.get("veto_active"))
        bad_cycles = int(previous.get("bad_cycles") or 0)
        healthy_cycles = int(previous.get("healthy_cycles") or 0)
        mature = [r for r in rows if int(r.get("realized_actions_floor") or 0) >= config.min_actions]

        if len(mature) < config.min_mature_notionals:
            states[wallet] = {
                **previous,
                "wallet_address": wallet,
                "status": "VETO_ACTIVE_EVIDENCE_STALE" if active_before else "EVIDENCE_ACCUMULATING",
                "veto_active": active_before,
                "mature_notionals": len(mature),
                "evaluated_at_ns": now_ns,
            }
            continue

        returns = [D(str(r.get("worst_latency_return_bps") or "0")) for r in mature]
        med = D(str(median(returns)))
        worst = min(returns)
        best = max(returns)
        safety_bad = [
            r for r in mature
            if SAFETY_BLOCKERS.intersection(set(r.get("promotion_blockers") or []))
        ]
        hard_safety = len(safety_bad) == len(mature)
        hard_negative = med <= config.hard_negative_bps
        all_negative = best < 0
        min_age = min(D(str(r.get("forward_age_hours_floor") or "0")) for r in mature)
        path_healthy = not safety_bad
        recovery_ready = (
            min_age >= config.recovery_age_hours
            and path_healthy
            and worst >= config.recovery_return_bps
        )

        reason = "FORWARD_HEALTHY"
        active_after = active_before
        if hard_safety:
            bad_cycles += 1
            healthy_cycles = 0
            active_after = True
            reason = "FORWARD_EMERGENCY_PATH_SAFETY"
        elif hard_negative:
            bad_cycles += 1
            healthy_cycles = 0
            active_after = True
            reason = "FORWARD_EMERGENCY_HARD_NEGATIVE"
        elif all_negative:
            bad_cycles += 1
            healthy_cycles = 0
            if active_before or bad_cycles >= config.persistent_negative_cycles:
                active_after = True
                reason = "FORWARD_EMERGENCY_PERSISTENT_NEGATIVE"
            else:
                active_after = False
                reason = "FORWARD_WATCH_NEGATIVE"
        else:
            bad_cycles = 0
            if active_before:
                if recovery_ready:
                    healthy_cycles += 1
                    if healthy_cycles >= config.recovery_cycles:
                        active_after = False
                        reason = "FORWARD_VETO_RELEASED"
                    else:
                        active_after = True
                        reason = "FORWARD_RECOVERY_PENDING"
                else:
                    healthy_cycles = 0
                    active_after = True
                    reason = "FORWARD_VETO_RETAINED_UNPROVEN_RECOVERY"
            else:
                healthy_cycles = 0
                active_after = False

        if active_after and not active_before:
            intervals.append(
                {
                    "wallet_address": wallet,
                    "coin": "*",
                    "effective_from_ns": now_ns,
                    "effective_until_ns": None,
                    "reason": reason,
                }
            )
        elif active_before and not active_after:
            _close_open_interval(intervals, wallet, now_ns)

        states[wallet] = {
            "wallet_address": wallet,
            "status": reason,
            "veto_active": active_after,
            "bad_cycles": bad_cycles,
            "healthy_cycles": healthy_cycles,
            "mature_notionals": len(mature),
            "worst_return_bps": str(worst),
            "median_return_bps": str(med),
            "best_return_bps": str(best),
            "minimum_forward_hours": str(min_age),
            "path_healthy": path_healthy,
            "evaluated_at_ns": now_ns,
        }

    return {
        "mode": "PROSPECTIVE_FORWARD_SHADOW_VETO_V1",
        "real_trading": False,
        "generated_at_ns": now_ns,
        "source_policy_id": path_truth.get("policy_id"),
        "wallet_states": states,
        "veto_intervals": intervals,
        "active_veto_count": sum(bool(row.get("veto_active")) for row in states.values()),
    }


def apply_veto_overlay_to_path_truth(
    path_truth: dict[str, Any], veto_result: dict[str, Any]
) -> dict[str, Any]:
    active = {
        wallet
        for wallet, state in (veto_result.get("wallet_states") or {}).items()
        if isinstance(state, dict) and bool(state.get("veto_active"))
    }
    promotion = []
    for raw in path_truth.get("promotion_candidates") or []:
        row = dict(raw)
        wallet = str(row.get("wallet_address") or "").lower()
        blockers = list(row.get("promotion_blockers") or [])
        if wallet in active:
            if VETO_BLOCKER not in blockers:
                blockers.append(VETO_BLOCKER)
            row["promotion_blockers"] = blockers
            row["validated_champion"] = False
            row["lifecycle_stage"] = "FORWARD_VETO_QUARANTINE"
        promotion.append(row)

    scenarios = []
    for raw in path_truth.get("scenario_candidates") or []:
        row = dict(raw)
        wallet = str(row.get("wallet_address") or "").lower()
        row["forward_veto_active"] = wallet in active
        scenarios.append(row)

    result = dict(path_truth)
    result["promotion_candidates"] = promotion
    result["scenario_candidates"] = scenarios
    result["forward_veto_active_count"] = len(active)
    result["forward_veto_mode"] = "QUARANTINE_CONTINUES_SHADOW_EVIDENCE"
    result["validated_champion_count"] = sum(
        bool(row.get("validated_champion")) for row in promotion
    )
    return result


def write_forward_veto_store(*, path_truth_path: Path, output_path: Path) -> dict[str, Any]:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("forward shadow veto refuses REAL_TRADING_ENABLED=YES")
    truth = json.loads(path_truth_path.read_text(encoding="utf-8"))
    if truth.get("real_trading") is not False:
        raise ValueError("path truth must be research-only")
    existing = _load(output_path)
    result = evaluate_forward_vetoes(
        path_truth=truth,
        existing=existing,
        now_ns=time.time_ns(),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    veto_tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    veto_tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    veto_tmp.replace(output_path)

    overlaid = apply_veto_overlay_to_path_truth(truth, result)
    truth_tmp = path_truth_path.with_suffix(path_truth_path.suffix + ".tmp")
    truth_tmp.write_text(json.dumps(overlaid, indent=2) + "\n", encoding="utf-8")
    truth_tmp.replace(path_truth_path)
    return result
