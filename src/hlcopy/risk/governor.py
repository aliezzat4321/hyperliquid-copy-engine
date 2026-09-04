from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from enum import IntEnum
from pathlib import Path
from typing import Any


class RiskState(IntEnum):
    NO_CAPITAL = 0
    MICRO_CANDIDATE = 1
    SMALL_CANDIDATE = 2
    SCALE_CANDIDATE = 3


EVIDENCE_CEILINGS = {
    "SHADOW_VALIDATED": RiskState.MICRO_CANDIDATE,
    "MICRO_LIVE_VALIDATED": RiskState.SMALL_CANDIDATE,
    "VALIDATED_LIVE": RiskState.SCALE_CANDIDATE,
}

REQUIRED_METRICS = {
    "max_drawdown_pct": "max_drawdown_pct",
    "rolling_drawdown_pct": "max_rolling_drawdown_pct",
    "max_single_trade_profit_share": "max_single_trade_profit_share",
    "max_single_trade_loss_share": "max_single_trade_loss_share",
    "max_trader_profit_share": "max_trader_profit_share",
    "max_trader_loss_share": "max_trader_loss_share",
    "max_coin_profit_share": "max_coin_profit_share",
    "max_coin_loss_share": "max_coin_loss_share",
    "max_leverage": "max_leverage",
    "min_liquidation_distance_pct": "min_liquidation_distance_pct",
    "max_margin_utilization_pct": "max_margin_utilization_pct",
    "max_correlated_wallet_exposure_share": "max_correlated_wallet_exposure_share",
    "max_correlated_trader_exposure_share": "max_correlated_trader_exposure_share",
    "max_correlated_coin_exposure_share": "max_correlated_coin_exposure_share",
    "decision_depth_usd": "minimum_decision_depth_usd",
    "estimated_capacity_usd": "minimum_capacity_usd",
    "latency_p95_ms": "max_latency_p95_ms",
    "staleness_p95_ms": "max_staleness_p95_ms",
    "rejected_signal_rate": "max_rejected_signal_rate",
    "tail_loss_p99_pct": "max_tail_loss_p99_pct",
    "max_adverse_excursion_pct": "max_adverse_excursion_pct",
    "unresolved_share": "max_unresolved_share",
    "open_exposure_usd": "max_open_exposure_usd",
    "max_holding_hours": "max_holding_hours",
}

LOWER_BOUND_METRICS = {
    "min_liquidation_distance_pct",
    "decision_depth_usd",
    "estimated_capacity_usd",
}


def _number(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _policy_path() -> Path:
    return Path(__file__).resolve().parents[3] / "docs/ai-team/promotion_policy.json"


def load_policy(path: Path | None = None) -> dict[str, Any]:
    payload = json.loads((path or _policy_path()).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("risk_governor"), dict):
        raise ValueError("promotion policy has no risk_governor object")
    return payload


def evaluate_risk_eligibility(
    evidence: dict[str, Any],
    *,
    previous_state: str = "NO_CAPITAL",
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the risk ceiling; this function cannot authorize or place real trades."""
    active_policy = policy or load_policy()
    governor = active_policy.get("risk_governor")
    if not isinstance(governor, dict) or not isinstance(governor.get("thresholds"), dict):
        raise ValueError("invalid risk_governor policy")
    thresholds = governor["thresholds"]
    blockers: list[str] = []

    level = evidence.get("evidence_level")
    ceiling = EVIDENCE_CEILINGS.get(str(level), RiskState.NO_CAPITAL)
    if level not in EVIDENCE_CEILINGS:
        blockers.append("EVIDENCE_LEVEL_NOT_ELIGIBLE")
    if evidence.get("evidence_auditor_pass") is not True:
        blockers.append("EVIDENCE_AUDITOR_NOT_PASS")
    if evidence.get("profitability_credible") is not True:
        blockers.append("CREDIBLE_EDGE_NOT_ESTABLISHED")
    if evidence.get("uncertainty_requirements_pass") is not True:
        blockers.append("UNCERTAINTY_REQUIREMENTS_NOT_PASS")

    for field, threshold_name in (
        ("closed_trades", "min_closed_trades"),
        ("distinct_days", "min_distinct_days"),
    ):
        value = _number(evidence.get(field))
        limit = _number(thresholds.get(threshold_name))
        if value is None or limit is None or value < 0:
            blockers.append(f"MISSING_OR_INVALID_{field.upper()}")
        elif value < limit:
            blockers.append(f"BELOW_{field.upper()}_MINIMUM")

    proposed = _number(evidence.get("proposed_notional_usd"))
    if proposed is None or proposed <= 0:
        blockers.append("MISSING_OR_INVALID_PROPOSED_NOTIONAL_USD")

    for field, threshold_name in REQUIRED_METRICS.items():
        value = _number(evidence.get(field))
        limit = _number(thresholds.get(threshold_name))
        if value is None or limit is None or value < 0:
            blockers.append(f"MISSING_OR_INVALID_{field.upper()}")
            continue
        violates = value < limit if field in LOWER_BOUND_METRICS else value > limit
        if violates:
            blockers.append(f"RISK_LIMIT_{field.upper()}")

    capacity = _number(evidence.get("estimated_capacity_usd"))
    depth = _number(evidence.get("decision_depth_usd"))
    if proposed is not None and proposed > 0:
        if capacity is not None and capacity < proposed:
            blockers.append("PROPOSED_NOTIONAL_EXCEEDS_CAPACITY")
        depth_multiple = _number(thresholds.get("min_depth_to_notional_multiple"))
        if depth is not None and depth_multiple is not None and depth < proposed * depth_multiple:
            blockers.append("INSUFFICIENT_DEPTH_FOR_PROPOSED_NOTIONAL")

    cost_evidence = evidence.get("cost_evidence")
    required_costs = ("fees", "slippage", "funding")
    if not isinstance(cost_evidence, dict):
        blockers.append("MISSING_COST_EVIDENCE")
    else:
        for cost in required_costs:
            if cost_evidence.get(cost) not in {"MODELED", "REALIZED", "NOT_APPLICABLE"}:
                blockers.append(f"MISSING_{cost.upper()}_EVIDENCE")
        if ceiling >= RiskState.SMALL_CANDIDATE:
            for cost in required_costs:
                if cost_evidence.get(cost) not in {"REALIZED", "NOT_APPLICABLE"}:
                    blockers.append(f"LIVE_{cost.upper()}_NOT_REALIZED")

    permitted = RiskState.NO_CAPITAL if blockers else ceiling
    try:
        previous = RiskState[str(previous_state)]
    except KeyError:
        previous = RiskState.NO_CAPITAL
        blockers.append("INVALID_PREVIOUS_STATE")
        permitted = RiskState.NO_CAPITAL

    critical = any(
        code.startswith("MISSING_")
        or code in {"EVIDENCE_AUDITOR_NOT_PASS", "INVALID_PREVIOUS_STATE"}
        for code in blockers
    )
    if permitted < previous:
        transition = "HALT" if critical else "DEMOTE"
    elif permitted > previous:
        transition = "ADVANCE"
    else:
        transition = "HOLD"

    return {
        "schema_version": "risk-eligibility-v1",
        "policy_id": active_policy.get("policy_id"),
        "policy_version": active_policy.get("policy_version"),
        "risk_governor_version": governor.get("version"),
        "previous_state": previous.name,
        "permitted_state": permitted.name,
        "transition": transition,
        "eligible": permitted != RiskState.NO_CAPITAL,
        "blockers": sorted(set(blockers)),
        "real_trading_authorized": False,
        "authorization_required": "LIVE_TRADING_GATE.md",
    }
