from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(name: str) -> str:
    return (ROOT / "deploy" / "systemd" / name).read_text(encoding="utf-8")


def test_shadow_capture_includes_l2_and_active_asset_context() -> None:
    unit = _text("hyperliquid-shadow-validation.service")
    assert "Environment=REAL_TRADING_ENABLED=NO" in unit
    assert "HLCOPY_MARKET_SUBSCRIPTION_TYPES=l2Book,activeAssetCtx" in unit


def test_selective_shadow_runs_causal_scorer_then_path_truth() -> None:
    unit = _text("hyperliquid-selective-shadow.service")
    assert "Environment=REAL_TRADING_ENABLED=NO" in unit
    assert "hlcopy.profitability.causal_selective_live_cli" in unit
    assert "hlcopy.profitability.selective_path_truth_fast_cli" in unit
    assert "selective_state_events.json" in unit
    assert "path_truth.json" in unit


def test_research_publishes_to_shadow_policy_store() -> None:
    unit = _text("hyperliquid-wallet-research.service")
    assert "Environment=REAL_TRADING_ENABLED=NO" in unit
    assert "--attribution" in unit
    assert "--policy-store" in unit
    assert "selective_policies.json" in unit


def test_timers_wait_for_prior_run_to_finish() -> None:
    research = _text("hyperliquid-wallet-research.timer")
    profitability = _text("hyperliquid-profitability.timer")
    selective = _text("hyperliquid-selective-shadow.timer")
    assert "OnUnitInactiveSec=1h" in research
    assert "OnUnitInactiveSec=1h" in profitability
    assert "OnUnitInactiveSec=15min" in selective
