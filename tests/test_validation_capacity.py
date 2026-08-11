from pathlib import Path

from hlcopy.research.cohort import CohortPolicy
from hlcopy.shadow.registry import MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP


def test_default_validation_cohort_uses_safe_per_ip_capacity() -> None:
    assert MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP == 10
    assert CohortPolicy().max_validation_wallets == MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP


def test_systemd_validation_cohort_uses_ten_wallet_capacity() -> None:
    unit = Path("deploy/systemd/hyperliquid-validation-cohort.service").read_text()
    assert "--max-validation-wallets 10" in unit
