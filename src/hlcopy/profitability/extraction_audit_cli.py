from __future__ import annotations

import argparse
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any

D = Decimal
ZERO = D("0")
BPS = D("10000")
SCENARIOS = ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS")


def _d(value: object, default: Decimal = ZERO) -> Decimal:
    try:
        return D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _key(row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(row.get("lane") or "UNKNOWN"),
        str(row.get("wallet_address") or "").lower(),
        str(row.get("coin") or "UNKNOWN"),
        str(row.get("direction") or "UNKNOWN"),
        str(row.get("action") or "UNKNOWN"),
        str(row.get("notional_usd") or "0"),
    )


def _ts(row: dict[str, Any]) -> int:
    return int(row.get("exchange_ts_ms") or 0)


def _slice_stats(rows: list[dict[str, Any]], notional: Decimal) -> dict[str, object]:
    pnl = [_d(row.get("net_pnl_usd")) for row in rows]
    total = sum(pnl, ZERO)
    wins = sum(x > ZERO for x in pnl)
    positive = sum((x for x in pnl if x > ZERO), ZERO)
    negative = sum((-x for x in pnl if x < ZERO), ZERO)
    timestamps = [_ts(row) for row in rows if _ts(row) > 0]
    span_days = (
        D(max(timestamps) - min(timestamps)) / D("86400000") if len(timestamps) >= 2 else ZERO
    )
    # Avoid explosive per-day estimates from a few minutes of evidence. A cohort has to
    # earn at least one day of denominator before expected dollars/day is reported.
    effective_days = max(D("1"), span_days)
    return_bps = total / notional * BPS if notional > ZERO else ZERO
    return {
        "actions": len(rows),
        "net_pnl_usd": str(total),
        "return_bps": str(return_bps),
        "return_pct": str(return_bps / D("100")),
        "avg_net_pnl_usd": str(total / D(len(rows))) if rows else None,
        "avg_net_bps": str(return_bps / D(len(rows))) if rows else None,
        "median_net_pnl_usd": str(median(pnl)) if pnl else None,
        "win_pct": str(D(wins) / D(len(rows)) * D("100")) if rows else None,
        "profit_factor": str(positive / negative) if negative > ZERO else ("Infinity" if positive > ZERO else None),
        "span_days": str(span_days),
        "trades_per_day": str(D(len(rows)) / effective_days) if rows else "0",
        "net_usd_per_day": str(total / effective_days),
        "first_ts_ms": min(timestamps) if timestamps else None,
        "last_ts_ms": max(timestamps) if timestamps else None,
    }


def build_audit(payload: dict[str, Any], *, train_fraction: Decimal = D("0.60")) -> dict[str, object]:
    raw = payload.get("realized_slices") or []
    slices = [row for row in raw if isinstance(row, dict)]
    grouped: dict[tuple[str, str, str, str, str, str], dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in slices:
        grouped[_key(row)][str(row.get("scenario") or "UNKNOWN")].append(row)

    cohorts: list[dict[str, object]] = []
    for key, by_scenario in grouped.items():
        lane, wallet, coin, direction, action, notional_text = key
        notional = _d(notional_text)
        complete = all(name in by_scenario for name in SCENARIOS)
        scenario_rows: dict[str, object] = {}
        train_returns: list[Decimal] = []
        oos_returns: list[Decimal] = []
        oos_usd_day: list[Decimal] = []
        train_actions: list[int] = []
        oos_actions: list[int] = []

        for scenario in SCENARIOS:
            rows = sorted(by_scenario.get(scenario, []), key=_ts)
            if not rows:
                continue
            split = int(D(len(rows)) * train_fraction)
            split = max(1, min(len(rows) - 1, split)) if len(rows) >= 2 else len(rows)
            train = rows[:split]
            oos = rows[split:]
            train_stats = _slice_stats(train, notional)
            oos_stats = _slice_stats(oos, notional)
            scenario_rows[scenario] = {"train": train_stats, "oos": oos_stats}
            train_returns.append(_d(train_stats["return_bps"]))
            oos_returns.append(_d(oos_stats["return_bps"]))
            oos_usd_day.append(_d(oos_stats["net_usd_per_day"]))
            train_actions.append(int(train_stats["actions"]))
            oos_actions.append(int(oos_stats["actions"]))

        train_floor = min(train_returns) if complete and train_returns else None
        oos_floor = min(oos_returns) if complete and oos_returns else None
        oos_usd_day_floor = min(oos_usd_day) if complete and oos_usd_day else None
        train_actions_floor = min(train_actions) if complete and train_actions else 0
        oos_actions_floor = min(oos_actions) if complete and oos_actions else 0

        if not complete:
            lifecycle = "UNRESOLVED"
            reason = "INCOMPLETE_LATENCY_SCENARIOS"
        elif oos_actions_floor < 10:
            lifecycle = "UNRESOLVED"
            reason = "INSUFFICIENT_OOS_ACTIONS"
        elif train_floor is None or train_floor <= ZERO:
            lifecycle = "DEAD"
            reason = "NON_POSITIVE_TRAIN_WORST_LATENCY"
        elif oos_floor is None or oos_floor <= ZERO:
            lifecycle = "DEAD"
            reason = "NON_POSITIVE_OOS_WORST_LATENCY"
        else:
            lifecycle = "SURVIVOR"
            reason = "POSITIVE_TRAIN_AND_OOS_ALL_LATENCIES"

        cohorts.append(
            {
                "lane": lane,
                "wallet_address": wallet,
                "coin": coin,
                "direction": direction,
                "action": action,
                "notional_usd": notional_text,
                "lifecycle": lifecycle,
                "reason": reason,
                "latency_complete": complete,
                "train_actions_floor": train_actions_floor,
                "oos_actions_floor": oos_actions_floor,
                "train_worst_return_bps": str(train_floor) if train_floor is not None else None,
                "oos_worst_return_bps": str(oos_floor) if oos_floor is not None else None,
                "oos_worst_return_pct": str(oos_floor / D("100")) if oos_floor is not None else None,
                "oos_worst_net_usd_per_day": str(oos_usd_day_floor) if oos_usd_day_floor is not None else None,
                "scenario_stats": scenario_rows,
            }
        )

    cohorts.sort(
        key=lambda row: (
            2 if row["lifecycle"] == "SURVIVOR" else 1 if row["lifecycle"] == "UNRESOLVED" else 0,
            _d(row.get("oos_worst_net_usd_per_day"), D("-1e99")),
            _d(row.get("oos_worst_return_bps"), D("-1e99")),
            int(row.get("oos_actions_floor") or 0),
        ),
        reverse=True,
    )

    survivors = [row for row in cohorts if row["lifecycle"] == "SURVIVOR"]
    unresolved = [row for row in cohorts if row["lifecycle"] == "UNRESOLVED"]
    dead = [row for row in cohorts if row["lifecycle"] == "DEAD"]

    keep_wallets = sorted({str(row["wallet_address"]) for row in survivors + unresolved})
    keep_coins = sorted({str(row["coin"]) for row in survivors + unresolved})
    dead_wallets = sorted({str(row["wallet_address"]) for row in dead} - set(keep_wallets))
    dead_coins = sorted({str(row["coin"]) for row in dead} - set(keep_coins))

    return {
        "mode": "PROFITABILITY_EXTRACTION_RESEARCH_ONLY",
        "real_trading": False,
        "source_generated_at": payload.get("generated_at"),
        "source_pnl_model": payload.get("pnl_model"),
        "fee_accounting_mode": payload.get("fee_accounting_mode"),
        "train_fraction": str(train_fraction),
        "oos_fraction": str(D("1") - train_fraction),
        "selection_rule": "SURVIVOR requires >=10 OOS actions in every latency scenario and positive train + OOS return in every latency scenario.",
        "slice_count": len(slices),
        "cohort_count": len(cohorts),
        "survivor_count": len(survivors),
        "unresolved_count": len(unresolved),
        "dead_count": len(dead),
        "top_survivors": survivors[:100],
        "top_unresolved": unresolved[:100],
        "dead_summary": {
            "count": len(dead),
            "wallets_with_no_surviving_or_unresolved_cohort": dead_wallets,
            "coins_with_no_surviving_or_unresolved_cohort": dead_coins,
        },
        "retention_manifest": {
            "keep_full_fidelity_wallets": keep_wallets,
            "keep_full_fidelity_coins": keep_coins,
            "eligible_for_raw_evidence_pruning_wallets": dead_wallets,
            "eligible_for_raw_evidence_pruning_coins": dead_coins,
            "automatic_delete": False,
            "note": "Pruning is only eligible after this compact audit is persisted and no other research dependency references the raw partition.",
        },
        "all_cohorts": cohorts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.profitability.extraction_audit_cli")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--train-fraction", type=Decimal, default=D("0.60"))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not D("0.50") <= args.train_fraction <= D("0.80"):
        raise SystemExit("--train-fraction must be between 0.50 and 0.80")
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    audit = build_audit(payload, train_fraction=args.train_fraction)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    tmp.replace(args.output)
    print(
        "profitability_extraction "
        f"slices={audit['slice_count']} cohorts={audit['cohort_count']} "
        f"survivors={audit['survivor_count']} unresolved={audit['unresolved_count']} "
        f"dead={audit['dead_count']} output={args.output}",
        flush=True,
    )
    for i, row in enumerate(audit["top_survivors"][:20], start=1):
        print(
            f"{i:2d}. {row['lane']:<6} {str(row['wallet_address'])[:14]} "
            f"{row['coin']:<14} {row['direction']:<5} {row['action']:<8} "
            f"${row['notional_usd']:<7} oos_actions={row['oos_actions_floor']:<3} "
            f"oos_worst={row['oos_worst_return_bps']}bps "
            f"usd_day={row['oos_worst_net_usd_per_day']}"
        )


if __name__ == "__main__":
    main()
