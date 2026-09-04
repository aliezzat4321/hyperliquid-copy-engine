from copy import deepcopy

from hlcopy.risk.governor import evaluate_risk_eligibility


def _valid_evidence() -> dict[str, object]:
    return {
        "evidence_level": "SHADOW_VALIDATED",
        "evidence_auditor_pass": True,
        "profitability_credible": True,
        "uncertainty_requirements_pass": True,
        "closed_trades": 60,
        "distinct_days": 10,
        "proposed_notional_usd": 50,
        "max_drawdown_pct": 5,
        "rolling_drawdown_pct": 4,
        "max_single_trade_profit_share": 0.2,
        "max_single_trade_loss_share": 0.2,
        "max_trader_profit_share": 0.3,
        "max_trader_loss_share": 0.3,
        "max_coin_profit_share": 0.3,
        "max_coin_loss_share": 0.3,
        "max_leverage": 2,
        "min_liquidation_distance_pct": 40,
        "max_margin_utilization_pct": 20,
        "max_correlated_wallet_exposure_share": 0.3,
        "max_correlated_trader_exposure_share": 0.3,
        "max_correlated_coin_exposure_share": 0.3,
        "decision_depth_usd": 5000,
        "estimated_capacity_usd": 500,
        "latency_p95_ms": 5000,
        "staleness_p95_ms": 6000,
        "rejected_signal_rate": 0.1,
        "tail_loss_p99_pct": 2,
        "max_adverse_excursion_pct": 3,
        "unresolved_share": 0.1,
        "open_exposure_usd": 25,
        "max_holding_hours": 48,
        "cost_evidence": {
            "fees": "MODELED",
            "slippage": "MODELED",
            "funding": "MODELED",
        },
    }


def test_valid_shadow_candidate_only_reaches_micro_candidate() -> None:
    result = evaluate_risk_eligibility(_valid_evidence())
    assert result["permitted_state"] == "MICRO_CANDIDATE"
    assert result["transition"] == "ADVANCE"
    assert result["real_trading_authorized"] is False
    assert result["policy_version"] == "v2"


def test_profitable_high_drawdown_candidate_is_denied() -> None:
    evidence = _valid_evidence()
    evidence["max_drawdown_pct"] = 25
    result = evaluate_risk_eligibility(evidence)
    assert result["permitted_state"] == "NO_CAPITAL"
    assert "RISK_LIMIT_MAX_DRAWDOWN_PCT" in result["blockers"]


def test_profitable_concentrated_candidate_is_denied() -> None:
    evidence = _valid_evidence()
    evidence["max_trader_profit_share"] = 0.9
    result = evaluate_risk_eligibility(evidence)
    assert result["permitted_state"] == "NO_CAPITAL"
    assert "RISK_LIMIT_MAX_TRADER_PROFIT_SHARE" in result["blockers"]


def test_missing_liquidity_or_unresolved_evidence_fails_closed() -> None:
    evidence = _valid_evidence()
    del evidence["decision_depth_usd"]
    del evidence["unresolved_share"]
    result = evaluate_risk_eligibility(evidence)
    assert result["permitted_state"] == "NO_CAPITAL"
    assert "MISSING_OR_INVALID_DECISION_DEPTH_USD" in result["blockers"]
    assert "MISSING_OR_INVALID_UNRESOLVED_SHARE" in result["blockers"]


def test_deterioration_automatically_demotes_shadow_candidate() -> None:
    evidence = _valid_evidence()
    evidence["rolling_drawdown_pct"] = 12
    result = evaluate_risk_eligibility(evidence, previous_state="MICRO_CANDIDATE")
    assert result["permitted_state"] == "NO_CAPITAL"
    assert result["transition"] == "DEMOTE"


def test_missing_auditor_result_halts_previously_eligible_candidate() -> None:
    evidence = _valid_evidence()
    del evidence["evidence_auditor_pass"]
    result = evaluate_risk_eligibility(evidence, previous_state="MICRO_CANDIDATE")
    assert result["permitted_state"] == "NO_CAPITAL"
    assert result["transition"] == "HALT"


def test_live_candidate_requires_realized_cost_evidence() -> None:
    evidence = deepcopy(_valid_evidence())
    evidence["evidence_level"] = "MICRO_LIVE_VALIDATED"
    result = evaluate_risk_eligibility(evidence)
    assert result["permitted_state"] == "NO_CAPITAL"
    assert "LIVE_FEES_NOT_REALIZED" in result["blockers"]
