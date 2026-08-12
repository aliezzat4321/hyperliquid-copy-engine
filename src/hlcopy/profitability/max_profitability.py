from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from statistics import median
from typing import Any

D = Decimal
ZERO = D("0")
ONE = D("1")
DEFAULT_MIN_EXECUTION_PCT = D("10")
MIN_WEIGHT = D("0.0001")


def _dec(value: object, default: str = "0") -> Decimal:
    if value is None:
        return D(default)
    return D(str(value))


def _scenario_latency_ms(scenario: object) -> int:
    text = str(scenario)
    if not text.startswith("LIVE_") or not text.endswith("MS"):
        return 10**9
    try:
        return int(text.removeprefix("LIVE_").removesuffix("MS"))
    except ValueError:
        return 10**9


def _evidence_adjusted_score(
    edge_bps: Decimal,
    evidence_weight: Decimal,
    execution_weight: Decimal,
) -> Decimal:
    weight = max(MIN_WEIGHT, evidence_weight * execution_weight)
    if edge_bps >= ZERO:
        return edge_bps * weight
    # Weak evidence must never make a losing configuration look less negative.
    return edge_bps / weight


def build_tournament(
    leverage_rows: list[dict[str, Any]],
    *,
    min_realized_actions: int = 10,
    min_execution_pct: Decimal = DEFAULT_MIN_EXECUTION_PCT,
    required_latencies_ms: tuple[int, ...] = (100, 250, 500, 1000),
) -> dict[str, object]:
    """Rank robust copy edge while leverage-path risk is still incomplete.

    Configurations are grouped by wallet/lane/notional/leverage and must survive every
    required latency slice. Until continuous MTM, funding, maintenance margin and
    liquidation paths exist, the primary tournament is deliberately leverage-neutral:
    it ranks the worst-latency *notional* return, then weights it by evidence and
    execution coverage. Levered ROE remains visible as an exploratory overlay only.
    """

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in leverage_rows:
        wallet = str(row.get("wallet_address", "")).lower()
        lane = str(row.get("lane", ""))
        notional = str(row.get("notional_usd", ""))
        leverage = str(row.get("follower_leverage", ""))
        if wallet and lane and notional and leverage:
            grouped[(wallet, lane, notional, leverage)].append(row)

    required = set(required_latencies_ms)
    all_configs: list[dict[str, object]] = []

    for (wallet, lane, notional, leverage), rows in grouped.items():
        by_latency = {
            _scenario_latency_ms(row.get("scenario")): row
            for row in rows
            if _scenario_latency_ms(row.get("scenario")) in required
        }
        if set(by_latency) != required:
            continue

        ordered = [by_latency[latency] for latency in sorted(required)]
        actions_floor = min(int(row.get("realized_actions", 0)) for row in ordered)
        execution_floor = min(_dec(row.get("execution_pct")) for row in ordered)
        if actions_floor < min_realized_actions or execution_floor < min_execution_pct:
            continue

        roes = [_dec(row.get("net_equity_return_pct")) for row in ordered]
        notional_edges = [_dec(row.get("net_notional_return_bps")) for row in ordered]
        pnl_values = [_dec(row.get("net_pnl_usd")) for row in ordered]
        equity_values = [_dec(row.get("equity_required_usd")) for row in ordered]

        worst_roe = min(roes)
        best_roe = max(roes)
        median_roe = D(str(median(roes)))
        latency_spread = best_roe - worst_roe
        worst_notional_edge = min(notional_edges)
        median_notional_edge = D(str(median(notional_edges)))

        evidence_weight = min(ONE, D(actions_floor) / D("50"))
        execution_weight = min(ONE, execution_floor / D("100"))
        robust_score = _evidence_adjusted_score(
            worst_notional_edge,
            evidence_weight,
            execution_weight,
        )

        worst_drawdown_pct = ZERO
        for row, equity in zip(ordered, equity_values, strict=True):
            if equity <= ZERO:
                continue
            raw_drawdown = _dec(row.get("max_closed_drawdown_usd"))
            drawdown = abs(raw_drawdown) / equity * D("100")
            worst_drawdown_pct = max(worst_drawdown_pct, drawdown)

        all_configs.append(
            {
                "wallet_address": wallet,
                "lane": lane,
                "notional_usd": notional,
                "follower_leverage": leverage,
                "required_latencies_ms": sorted(required),
                "realized_actions_floor": actions_floor,
                "execution_pct_floor": str(execution_floor),
                "worst_latency_notional_return_bps": str(worst_notional_edge),
                "median_latency_notional_return_bps": str(median_notional_edge),
                "worst_latency_roe_pct": str(worst_roe),
                "median_latency_roe_pct": str(median_roe),
                "best_latency_roe_pct": str(best_roe),
                "latency_roe_spread_pct": str(latency_spread),
                "worst_closed_drawdown_on_equity_pct": str(worst_drawdown_pct),
                "median_equity_required_usd": str(D(str(median(equity_values)))),
                "median_net_pnl_usd": str(D(str(median(pnl_values)))),
                "robust_profitability_score": str(robust_score),
                "primary_rank_is_leverage_neutral": True,
                "levered_roe_is_exploratory": True,
                "research_only": True,
                "live_eligible": False,
                "risk_truth_status": (
                    "BLOCKED_PENDING_CONTINUOUS_MTM_FUNDING_MAINTENANCE_MARGIN_"
                    "AND_PATH_LIQUIDATION"
                ),
            }
        )

    all_configs.sort(
        key=lambda row: (
            _dec(row["robust_profitability_score"]),
            _dec(row["worst_latency_notional_return_bps"]),
            int(row["realized_actions_floor"]),
            _dec(row["execution_pct_floor"]),
            -_dec(row["follower_leverage"]),
        ),
        reverse=True,
    )

    # Before path-risk truth exists, multiple leverage rows for the same underlying
    # copy configuration are denominator variants, not independent strategy edges.
    ranked: list[dict[str, object]] = []
    seen_underlying: set[tuple[str, str, str]] = set()
    for row in all_configs:
        key = (
            str(row["wallet_address"]),
            str(row["lane"]),
            str(row["notional_usd"]),
        )
        if key in seen_underlying:
            continue
        seen_underlying.add(key)
        ranked.append(row)

    leveraged_exploration = sorted(
        all_configs,
        key=lambda row: (
            _dec(row["worst_latency_roe_pct"]),
            _dec(row["robust_profitability_score"]),
            int(row["realized_actions_floor"]),
        ),
        reverse=True,
    )

    best_by_wallet: dict[str, dict[str, object]] = {}
    for row in ranked:
        best_by_wallet.setdefault(str(row["wallet_address"]), row)

    return {
        "objective": (
            "maximize robust prospective underlying copy edge now; maximize levered "
            "net ROE with no arbitrary return ceiling once path-risk truth is modeled"
        ),
        "primary_ranking_basis": (
            "worst-latency net notional return weighted by evidence and execution; "
            "leverage-neutral until continuous path risk is modeled"
        ),
        "required_latencies_ms": sorted(required),
        "min_realized_actions": min_realized_actions,
        "min_execution_pct": str(min_execution_pct),
        "risk_truth_status": (
            "RESEARCH_ONLY_UNTIL_CONTINUOUS_MTM_FUNDING_MAINTENANCE_MARGIN_"
            "AND_PATH_LIQUIDATION_ARE_MODELED"
        ),
        "candidate_count": len(all_configs),
        "primary_ranked_count": len(ranked),
        "ranked": ranked,
        "best_by_wallet": list(best_by_wallet.values()),
        "leveraged_exploration": leveraged_exploration,
    }
