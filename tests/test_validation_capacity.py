from pathlib import Path

from hlcopy.research.cohort import CohortPolicy
from hlcopy.shadow.registry import MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP


def test_default_validation_cohort_uses_safe_per_ip_capacity() -> None:
    assert MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP == 10
    assert CohortPolicy().max_validation_wallets == MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP


def test_systemd_validation_cohort_uses_ten_wallet_capacity_and_full_prewarm() -> None:
    unit = Path("deploy/systemd/hyperliquid-validation-cohort.service").read_text()
    assert "--max-validation-wallets 10" in unit
    assert "--max-seed-coins 200" in unit


def test_shadow_service_uses_l2_only_for_broad_market_prewarm() -> None:
    unit = Path("deploy/systemd/hyperliquid-shadow-validation.service").read_text()
    assert "HLCOPY_MARKET_SUBSCRIPTION_TYPES=l2Book" in unit
    assert "REAL_TRADING_ENABLED=NO" in unit
