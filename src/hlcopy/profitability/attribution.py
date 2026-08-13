from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

D = Decimal
ZERO = D("0")
BPS = D("10000")
REQUIRED_SCENARIOS = (
    "LIVE_100MS",
    "LIVE_250MS",
    "LIVE_500MS",
    "LIVE_1000MS",
)


def _decimal(value: object, default: Decimal = ZERO) -> Decimal:
    try:
        return D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _group_key(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(row.get("lane") or "UNKNOWN"),
        str(row.get("wallet_address") or "").lower(),
        str(row.get("coin") or "UNKNOWN"),
        str(row.get("direction") or "UNKNOWN"),
        str(row.get("action") or "UNKNOWN"),
        str(row.get("notional_usd") or "0"),
    )


def _stats(rows: Iterable[dict[str, Any]], *, notional_usd: Decimal) -> dict[str, object]:
    items = list(rows)
    pnl = sum((_decimal(row.get("net_pnl_usd")) for row in items), ZERO)
    gross = sum((_decimal(row.get("gross_pnl_usd")) for row in items), ZERO)
    fees = sum((_decimal(row.get("fee_usd")) for row in items), ZERO)
    wins = sum(_decimal(row.get("net_pnl_usd")) > ZERO for row in items)
    timestamps = [
        int(row["exchange_ts_ms"])
        for row in items
        if row.get("exchange_ts_ms") is not None
    ]
    feed_values = [
        _decimal(row.get("feed_ms"))
        for row in items
        if row.get("feed_ms") is not None
    ]
    return_bps = pnl / notional_usd * BPS if notional_usd > ZERO else ZERO
    return {
        "actions": len(items),
        "wins": wins,
        "win_pct": str(D(wins) / D(len(items)) * D("100")) if items else None,
        "net_pnl_usd": str(pnl),
        "gross_pnl_usd": str(gross),
        "fees_usd": str(fees),
        "return_bps": str(return_bps),
        "avg_net_pnl_usd": str(pnl / D(len(items))) if items else None,
        "avg_feed_ms": str(sum(feed_values, ZERO) / D(len(feed_values))) if feed_values else None,
        "first_exchange_ts_ms": min(timestamps) if timestamps else None,
        "last_exchange_ts_ms": max(timestamps) if timestamps else None,
    }


def build_trade_attribution(payload: dict[str, Any]) -> dict[str, object]:
    """Build descriptive, research-only P&L attribution from realized copy slices.

    The output is intentionally not a trading rule. It groups already-realized prospective
    outcomes to identify hypotheses for a later chronological selective-copy optimizer.
    No cohort is considered latency-robust unless all required live latency scenarios exist.
    """

    raw_slices = payload.get("realized_slices")
    slices = [row for row in raw_slices or [] if isinstance(row, dict)]
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in slices:
        grouped[_group_key(row)].append(row)

    cohorts: list[dict[str, object]] = []
    for key, rows in grouped.items():
        lane, wallet, coin, direction, action, notional_text = key
        notional = _decimal(notional_text)
        by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_scenario[str(row.get("scenario") or "UNKNOWN")].append(row)

        scenario_stats = {
            scenario: _stats(items, notional_usd=notional)
            for scenario, items in sorted(by_scenario.items())
        }
        complete = all(name in scenario_stats for name in REQUIRED_SCENARIOS)
        required = [scenario_stats[name] for name in REQUIRED_SCENARIOS if name in scenario_stats]
        robust_return = (
            min(_decimal(item["return_bps"]) for item in required)
            if complete
            else None
        )
        robust_actions = (
            min(int(item["actions"]) for item in required)
            if complete
            else 0
        )
        robust_win = (
            min(_decimal(item["win_pct"]) for item in required if item["win_pct"] is not None)
            if complete and any(item["win_pct"] is not None for item in required)
            else None
        )
        cohorts.append(
            {
                "lane": lane,
                "wallet_address": wallet,
                "coin": coin,
                "direction": direction,
                "action": action,
                "notional_usd": notional_text,
                "scenario_count": len(scenario_stats),
                "latency_complete": complete,
                "robust_return_bps": str(robust_return) if robust_return is not None else None,
                "robust_actions_floor": robust_actions,
                "robust_win_pct_floor": str(robust_win) if robust_win is not None else None,
                "scenario_stats": scenario_stats,
            }
        )

    complete_cohorts = [row for row in cohorts if row["latency_complete"]]
    complete_cohorts.sort(
        key=lambda row: (
            _decimal(row["robust_return_bps"]),
            int(row["robust_actions_floor"]),
        ),
        reverse=True,
    )
    incomplete_cohorts = [row for row in cohorts if not row["latency_complete"]]
    incomplete_cohorts.sort(
        key=lambda row: (str(row["wallet_address"]), str(row["coin"]), str(row["action"]))
    )

    return {
        "generated_from": payload.get("generated_at"),
        "source_pnl_model": payload.get("pnl_model"),
        "mode": "DESCRIPTIVE_RESEARCH_ONLY_NO_AUTOMATIC_FILTER_PROMOTION",
        "causal_note": (
            "Inputs are prospective realized follower slices. Attribution uses realized "
            "outcomes only to generate research hypotheses; these outcomes must never be "
            "used as same-period entry features. Any selective-copy rule must be trained "
            "and validated chronologically."
        ),
        "required_latency_scenarios": list(REQUIRED_SCENARIOS),
        "slice_count": len(slices),
        "cohort_count": len(cohorts),
        "latency_complete_cohort_count": len(complete_cohorts),
        "ranked_complete_cohorts": complete_cohorts,
        "incomplete_cohorts": incomplete_cohorts,
        "real_trading": False,
    }
