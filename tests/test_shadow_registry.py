from pathlib import Path

import pytest

from hlcopy.shadow.registry import WalletRegistry, WalletSpec


ADDRESS = "0x1111111111111111111111111111111111111111"


def test_registry_lifecycle_is_explicit_and_atomic(tmp_path: Path):
    path = tmp_path / "wallets.json"
    registry = WalletRegistry(path)
    registry.init()
    assert registry.load() == ()

    stored = registry.add(
        WalletSpec(
            id="alpha",
            label="Alpha",
            source_type="hyperliquid_wallet",
            source_ref=ADDRESS,
            coins=("btc", "ETH", "btc"),
        )
    )
    assert stored.stage == "research"
    assert stored.coins == ("BTC", "ETH")
    assert registry.active_hyperliquid_wallets() == ()

    validated = registry.update("alpha", stage="validation")
    assert validated.stage == "validation"
    assert registry.active_hyperliquid_wallets() == (validated,)
    assert registry.market_coins() == ("BTC", "ETH")

    approved = registry.update("alpha", stage="approved")
    assert approved.stage == "approved"
    assert registry.active_hyperliquid_wallets() == (approved,)

    registry.update("alpha", enabled=False)
    assert registry.active_hyperliquid_wallets() == ()


def test_registry_supports_external_research_sources(tmp_path: Path):
    registry = WalletRegistry(tmp_path / "wallets.json")
    wallet = registry.add(
        WalletSpec(
            id="bones",
            label="Bones",
            source_type="external",
            source_ref="invo:85a9ca4c-4f45-4c8e-b1ae-e41c4d193971",
            coins=("BTC", "ETH", "HYPE", "NEAR", "SOL"),
            notes="Research reference only; no automated private endpoint access.",
        )
    )
    assert wallet.stage == "research"
    assert registry.active_hyperliquid_wallets() == ()


def test_invalid_hyperliquid_address_fails_closed():
    with pytest.raises(ValueError, match="42-character"):
        WalletSpec(
            id="bad",
            label="Bad",
            source_type="hyperliquid_wallet",
            source_ref="0x123",
        )
