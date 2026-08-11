from pathlib import Path

from hlcopy.research.cohort import CohortPolicy
from hlcopy.shadow.registry import MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP


def test_default_validation_cohort_uses_safe_per_ip_capacity() -> None:
    assert MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP == 10
    assert CohortPolicy().max_validation_wallets == MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP


def test_systemd_validation_cohort_publishes_full_active_market_universe() -> None:
    unit = Path("deploy/systemd/hyperliquid-validation-cohort.service").read_text()
    assert "--max-validation-wallets 10" in unit
    assert "--max-seed-coins 200" in unit
    assert "EnvironmentFile=/root/hlcopy-db.env" in unit
    assert "--market-universe-out" in unit
    assert "active_perp_markets.txt" in unit


def test_shadow_service_pins_validation_critical_market_config_on_execstart() -> None:
    unit = Path("deploy/systemd/hyperliquid-shadow-validation.service").read_text()
    exec_start = unit.split("ExecStart=", 1)[1].split("Restart=", 1)[0]

    assert exec_start.startswith("/usr/bin/env")
    assert "HLCOPY_MARKET_SUBSCRIPTION_TYPES=l2Book" in exec_start
    assert "HLCOPY_MARKET_FLUSH_SECONDS=300" in exec_start
    assert "HLCOPY_MARKET_FLUSH_ROWS=100000" in exec_start
    assert "--coins-file" in exec_start
    assert "active_perp_markets.txt" in exec_start
    assert "REAL_TRADING_ENABLED=NO" in unit
