from __future__ import annotations

from pathlib import Path

import pytest

from hlcopy.shadow.latency import LatencyScenario, ObservedSignalLatency
from hlcopy.shadow.policy import ValidationEvidence, ValidationPolicy, evaluate_validation
from hlcopy.shadow.registry import WalletRegistry, WalletSpec
from hlcopy.trading.permissions import TradingPermissionError, assert_source_trade_allowed

ADDRESS = "0x1111111111111111111111111111111111111111"


def _evidence(**overrides) -> ValidationEvidence:
    values = {
        "source_id": "alpha",
        "prospective": True,
        "completed_trades": 50,
        "observed_days": 14.0,
        "execution_attempts": 50,
        "executed_trades": 49,
        "avg_net_return_pct": 1.2,
        "median_net_return_pct": 0.8,
        "mean_return_lower_bound_pct": 0.2,
        "profit_factor": 1.8,
        "max_drawdown_pct": 4.0,
        "worst_trade_pct": -8.0,
        "p95_signal_feed_lag_ms": 500.0,
        "market_gap_fraction": 0.001,
        "avg_net_return_1s_pct": 0.7,
        "avg_net_return_5s_pct": 0.2,
        "funding_modeled": True,
        "liquidation_path_modeled": True,
        "evidence_fingerprint": "abc123",
    }
    values.update(overrides)
    return ValidationEvidence(**values)


def test_validation_gate_never_confuses_source_history_with_prospective_evidence():
    decision = evaluate_validation(_evidence(prospective=False))
    assert not decision.eligible_for_human_approval
    assert "NOT_PROSPECTIVE" in decision.failures


def test_validation_gate_can_mark_strong_prospective_evidence_eligible_for_review():
    decision = evaluate_validation(_evidence(), ValidationPolicy())
    assert decision.eligible_for_human_approval
    assert decision.status == "ELIGIBLE_FOR_HUMAN_APPROVAL"
    assert decision.failures == ()


def test_latency_keeps_feed_and_order_path_separate():
    observed = ObservedSignalLatency(
        exchange_ts_ms=1_000,
        local_received_at_ns=1_250_000_000,
    )
    scenario = LatencyScenario(
        "measured",
        decision_ms=10.0,
        outbound_order_ms=25.0,
        exchange_processing_ms=5.0,
    )
    assert observed.feed_ms == 250.0
    assert observed.estimated_order_arrival_ms(scenario) == 1_290.0


def test_implausible_exchange_clock_delta_fails_closed():
    observed = ObservedSignalLatency(
        exchange_ts_ms=10_000,
        local_received_at_ns=1_000_000_000,
    )
    with pytest.raises(ValueError, match="implausible"):
        observed.estimated_order_arrival_ms(
            LatencyScenario("measured", decision_ms=1.0, outbound_order_ms=1.0)
        )


def test_future_trading_gate_requires_both_global_switch_and_approved_source(tmp_path: Path):
    registry = WalletRegistry(tmp_path / "wallets.json")
    registry.add(
        WalletSpec(
            id="alpha",
            label="Alpha",
            source_type="hyperliquid_wallet",
            source_ref=ADDRESS,
            stage="validation",
            coins=("BTC",),
        )
    )
    with pytest.raises(TradingPermissionError, match="not YES"):
        assert_source_trade_allowed(
            registry=registry,
            source_id="alpha",
            real_trading_enabled="NO",
        )
    with pytest.raises(TradingPermissionError, match="expected 'approved'"):
        assert_source_trade_allowed(
            registry=registry,
            source_id="alpha",
            real_trading_enabled="YES",
        )

    registry.update("alpha", stage="approved")
    assert_source_trade_allowed(
        registry=registry,
        source_id="alpha",
        real_trading_enabled="YES",
    )
