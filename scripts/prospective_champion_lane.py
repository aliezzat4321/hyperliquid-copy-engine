#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hlcopy.profitability.causal_book import CausalParquetL2BookProvider
from hlcopy.profitability.lane1_handoff import record_prospective_outcomes
from hlcopy.profitability.portfolio_position_copy import simulate_copy_with_portfolio_capital
from hlcopy.profitability.position_copy import load_wide_events
from hlcopy.profitability.position_live_cli import NOTIONALS, SCENARIOS, _summary

D = Decimal
BPS = D("10000")
BASE = Path("/root/hyperliquid-audit/prospective-champions")
CFG = BASE / "config.json"
REPORT = BASE / "report.json"
DEFAULT_QUEUE = Path("/root/hyperliquid-audit/funnel/challenger_queue.json")
WIDE = Path("/mnt/HC_Volume_106576526/hyperliquid/shadow/wide-enriched-live")
MARKET = Path("/mnt/HC_Volume_106576526/hyperliquid/market-shadow")
MIN_EVALUATED_ACTIONS = 20


def atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_challenger_queue(queue_path: Path):
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    targets = []
    for row in payload.get("candidates", []):
        if row.get("status") != "challenger":
            continue
        targets.append(
            {
                "wallet": str(row["wallet_address"]).lower(),
                "coin": str(row["coin"]),
                "primary_notional": str(row["notional_usd"]),
                "prospective_start_ns": int(row["prospective_start_ns"]),
                "candidate_key": str(row["candidate_key"]),
            }
        )
    return payload, targets


def load_frozen_targets(queue_path: Path):
    return load_challenger_queue(queue_path)[1]


def _selection_return_bps(summary: dict[str, object], notional: Decimal) -> Decimal | None:
    """Average realized net return per action, not cumulative PnL / one trade notional.

    The old metric increased mechanically with action count and observation-window length.
    This denominator makes the selection statistic comparable across candidates while the
    report continues to expose cumulative closed PnL separately.
    """
    actions = int(summary.get("realized_actions") or 0)
    if actions <= 0 or notional <= 0:
        return None
    net_pnl = D(str(summary.get("closed_net_pnl_usd") or "0"))
    return net_pnl / (notional * D(actions)) * BPS


def _fingerprint(outcome: dict[str, object]) -> str:
    stable = {
        key: outcome.get(key)
        for key in (
            "candidate_key",
            "event_count",
            "evaluation_state",
            "actions_floor",
            "worst_primary_return_bps",
            "approved",
        )
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def main() -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").upper() == "YES":
        raise SystemExit("REAL_TRADING_ENABLED must remain NO")
    ap = argparse.ArgumentParser()
    ap.add_argument("--challenger-queue", type=Path, default=DEFAULT_QUEUE)
    args = ap.parse_args()
    BASE.mkdir(parents=True, exist_ok=True)
    if not args.challenger_queue.exists():
        raise SystemExit(f"challenger queue missing: {args.challenger_queue}")

    queue, targets = load_challenger_queue(args.challenger_queue)
    observed_ns = time.time_ns()
    observed_at = datetime.now(UTC).isoformat()
    atomic(
        CFG,
        {
            "mode": "AUTONOMOUS_FROZEN_PROSPECTIVE_V3",
            "updated_ns": observed_ns,
            "source_queue": str(args.challenger_queue),
            "canonical_notionals": [str(value) for value in NOTIONALS],
            "targets": targets,
            "real_trading": False,
        },
    )

    cutoff = min(
        (int(target["prospective_start_ns"]) for target in targets),
        default=observed_ns,
    )
    events = load_wide_events(WIDE, cutoff_ns=cutoff)
    grouped = defaultdict(list)
    for event in events:
        grouped[(event.wallet_address.lower(), event.coin)].append(event)

    rows: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []
    for target_spec in targets:
        prospective_start_ns = int(target_spec["prospective_start_ns"])
        target_events = tuple(
            event
            for event in grouped.get(
                (target_spec["wallet"].lower(), target_spec["coin"]), []
            )
            if event.received_at_ns >= prospective_start_ns
        )
        target: dict[str, object] = {
            "candidate_key": target_spec["candidate_key"],
            "wallet_address": target_spec["wallet"],
            "coin": target_spec["coin"],
            "primary_notional": target_spec["primary_notional"],
            "event_count": len(target_events),
            "scenarios": [],
        }
        if target_events:
            for scenario in SCENARIOS:
                provider = CausalParquetL2BookProvider(MARKET)
                provider.prime(target_events, (scenario,))
                for notional in NOTIONALS:
                    sim = simulate_copy_with_portfolio_capital(
                        target_events,
                        provider=provider,
                        scenario=scenario,
                        notional_usd=notional,
                        taker_fee_bps=D("4.5"),
                        max_slippage_bps=D("20"),
                        max_book_forward_ms=750,
                    )
                    summary = _summary(sim)
                    selection_return = _selection_return_bps(summary, notional)
                    target["scenarios"].append(
                        {
                            "scenario": scenario.name,
                            "notional_usd": str(notional),
                            "realized_actions": int(summary["realized_actions"]),
                            "selection_return_bps": (
                                str(selection_return) if selection_return is not None else None
                            ),
                            "closed_net_pnl_usd": str(summary["closed_net_pnl_usd"]),
                            "legacy_cumulative_return_bps": str(summary["net_return_bps"]),
                        }
                    )

        primary = [
            row
            for row in target["scenarios"]
            if row["notional_usd"] == target_spec["primary_notional"]
        ]
        if not primary:
            target["evaluation_state"] = "NOT_EVALUATED"
            target["worst_primary_return_bps"] = None
            target["actions_floor"] = None
            target["approved"] = None
        else:
            actions_floor = min(int(row["realized_actions"]) for row in primary)
            measured_returns = [
                D(str(row["selection_return_bps"]))
                for row in primary
                if row["selection_return_bps"] is not None
            ]
            target["actions_floor"] = actions_floor
            if actions_floor < MIN_EVALUATED_ACTIONS or len(measured_returns) != len(primary):
                target["evaluation_state"] = "INSUFFICIENT_ACTIONS"
                target["worst_primary_return_bps"] = (
                    str(min(measured_returns)) if measured_returns else None
                )
                target["approved"] = None
            else:
                worst = min(measured_returns)
                target["evaluation_state"] = "EVALUATED"
                target["worst_primary_return_bps"] = str(worst)
                target["approved"] = worst > 0
        rows.append(target)

        outcome: dict[str, object] = {
            "candidate_key": target_spec["candidate_key"],
            "observed_at": observed_at,
            "prospective_start_ns": prospective_start_ns,
            "event_count": target["event_count"],
            "evaluation_state": target["evaluation_state"],
            "actions_floor": target["actions_floor"],
            "worst_primary_return_bps": target["worst_primary_return_bps"],
            "approved": target["approved"],
            "return_basis": "AVERAGE_REALIZED_NET_PNL_PER_ACTION_OVER_TARGET_NOTIONAL_V1",
        }
        outcome["evidence_fingerprint"] = _fingerprint(outcome)
        outcomes.append(outcome)

    report = {
        "mode": "AUTONOMOUS_CLEAN_PROSPECTIVE_LANE_V3",
        "cutoff_ns": cutoff,
        "age_hours": (time.time_ns() - cutoff) / 3.6e12,
        "real_trading": False,
        "challenger_queue_generated_at": queue.get("generated_at"),
        "challenger_queue_counts": queue.get("counts", {}),
        "canonical_notionals": [str(value) for value in NOTIONALS],
        "return_basis": "AVERAGE_REALIZED_NET_PNL_PER_ACTION_OVER_TARGET_NOTIONAL_V1",
        "targets": rows,
        "challenger_count": len(targets),
        "prospective_shadow_count": sum(1 for row in rows if row["event_count"] > 0),
        "evaluated_count": sum(1 for row in rows if row["evaluation_state"] == "EVALUATED"),
        "insufficient_evidence_count": sum(
            1 for row in rows if row["evaluation_state"] != "EVALUATED"
        ),
        "approved_count": sum(1 for row in rows if row["approved"] is True),
        "rejections": queue.get("rejections", [])
        or (
            []
            if targets
            else [{"reason": "NO_ACTIVE_CHALLENGERS", "timestamp_ns": time.time_ns()}]
        ),
        "demoted": queue.get("demoted", []),
    }
    atomic(REPORT, report)
    record_prospective_outcomes(args.challenger_queue, outcomes)

    print("=== PROSPECTIVE CHAMPIONS ===")
    print(
        "age_hours=",
        round(report["age_hours"], 3),
        "evaluated=",
        report["evaluated_count"],
        "approved=",
        report["approved_count"],
    )
    for row in rows:
        print(
            row["wallet_address"][:14],
            row["coin"],
            "events=",
            row["event_count"],
            "state=",
            row["evaluation_state"],
            "actions_floor=",
            row["actions_floor"],
            "worst_primary_bps=",
            row["worst_primary_return_bps"],
            "APPROVED=",
            row["approved"],
        )
    print("report=", REPORT)


if __name__ == "__main__":
    main()
