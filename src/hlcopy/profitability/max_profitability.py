from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from statistics import median
from typing import Any

D = Decimal
ZERO = D("0")
ONE = D("1")


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


def build_tournament(
    leverage_rows: list[dict[str, Any]],
    *,
    min_realized_actions: int = 10,
    min_execution_pct: Decimal = D("10"),
    required_latencies_ms: tuple[int, ...] = (100, 250, 500, 1000),
) -> dict[str, object]:
    """Rank copy configurations by robust prospective return on equity.

    Configurations are grouped by wallet/lane/notional/leverage and must survive every
    required latency slice. The tournament maximizes the worst observed latency ROE,
    then rewards evidence and execution coverage. It deliberately does not infer
    liquidation safety, funding, or live approval from leverage-adjusted ROE.
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
    candidates: list[dict[str, object]] = []

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
        pnl_values = [_dec(row.get("net_pnl_usd")) for row in ordered]
        equity_values = [_dec(row.get("equity_required_usd")) for row in ordered]
        worst_roe = min(roes)
        best_roe = max(roes)
        median_roe = D(str(median(roes)))
        latency_spread = best_roe - worst_roe

        evidence_weight = min(ONE, D(actions_floor) / D("50"))
        execution_weight = min(ONE, execution_floor / D("100"))
        robust_score = worst_roe * evidence_weight * execution_weight

        worst_drawdown_pct = ZERO
        for row, equity in zip(ordered, equity_values, strict=True):
            if equity <= ZERO:
                continue
            raw_drawdown = _dec(row.get("max_closed_drawdown_usd"))
            drawdown = abs(raw_drawdown) / equity * D("100")
            worst_drawdown_pct = max(worst_drawdown_pct, drawdown)

        candidates.append(
            {
                "wallet_address": wallet,
                "lane": lane,
                "notional_usd": notional,
                "follower_leverage": leverage,
                "required_latencies_ms": sorted(required),
                "realized_actions_floor": actions_floor,
                "execution_pct_floor": str(execution_floor),
                "worst_latency_roe_pct": str(worst_roe),
                "median_latency_roe_pct": str(median_roe),
                "best_latency_roe_pct": str(best_roe),
                "latency_roe_spread_pct": str(latency_spread),
                "worst_closed_drawdown_on_equity_pct": str(worst_drawdown_pct),
                "median_equity_required_usd": str(D(str(median(equity_values)))),
                "median_net_pnl_usd": str(D(str(median(pnl_values)))),
                "robust_profitability_score": str(robust_score),
                "research_only": True,
                "live_eligible": False,
                "risk_truth_status": (
                    "BLOCKED_PENDING_CONTINUOUS_MTM_FUNDING_MAINTENANCE_MARGIN_"
                    "AND_PATH_LIQUIDATION"
                ),
            }
        )

    candidates.sort(
        key=lambda row: (
            _dec(row["robust_profitability_score"]),
            _dec(row["worst_latency_roe_pct"]),
            int(row["realized_actions_floor"]),
            _dec(row["execution_pct_floor"]),
        ),
        reverse=True,
    )

    best_by_wallet: dict[str, dict[str, object]] = {}
    for row in candidates:
        best_by_wallet.setdefault(str(row["wallet_address"]), row)

    return {
        "objective": (
            "maximize worst-latency prospective net ROE, weighted by evidence and "
            "execution coverage; no arbitrary return ceiling"
        ),
        "required_latencies_ms": sorted(required),
        "min_realized_actions": min_realized_actions,
        "min_execution_pct": str(min_execution_pct),
        "risk_truth_status": (
            "RESEARCH_ONLY_UNTIL_CONTINUOUS_MTM_FUNDING_MAINTENANCE_MARGIN_"
            "AND_PATH_LIQUIDATION_ARE_MODELED"
        ),
        "candidate_count": len(candidates),
        "ranked": candidates,
        "best_by_wallet": list(best_by_wallet.values()),
    }
