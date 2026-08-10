from __future__ import annotations

from pathlib import Path

import pytest

from hlcopy.shadow.capture import required_market_coins
from hlcopy.shadow.registry import WalletRegistry, WalletSpec

ADDRESS = "0x1111111111111111111111111111111111111111"
ADDRESS_2 = "0x2222222222222222222222222222222222222222"


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


def test_validation_wallet_requires_explicit_coin_coverage(tmp_path: Path):
    registry = WalletRegistry(tmp_path / "wallets.json")
    registry.add(
        WalletSpec(
            id="alpha",
            label="Alpha",
            source_type="hyperliquid_wallet",
            source_ref=ADDRESS,
        )
    )
    with pytest.raises(ValueError, match="explicit market coins"):
        registry.update("alpha", stage="validation")


def test_duplicate_hyperliquid_address_is_rejected(tmp_path: Path):
    registry = WalletRegistry(tmp_path / "wallets.json")
    registry.add(
        WalletSpec(
            id="alpha",
            label="Alpha",
            source_type="hyperliquid_wallet",
            source_ref=ADDRESS,
        )
    )
    with pytest.raises(ValueError, match="already exists"):
        registry.add(
            WalletSpec(
                id="beta",
                label="Beta",
                source_type="hyperliquid_wallet",
                source_ref=ADDRESS.upper().replace("0X", "0x"),
            )
        )


def test_market_universe_changes_with_registry_without_code_changes(tmp_path: Path):
    registry = WalletRegistry(tmp_path / "wallets.json")
    registry.add(
        WalletSpec(
            id="alpha",
            label="Alpha",
            source_type="hyperliquid_wallet",
            source_ref=ADDRESS,
            stage="validation",
            coins=("BTC", "ETH"),
        )
    )
    assert required_market_coins(registry, ("SOL",)) == ("SOL", "BTC", "ETH")

    registry.add(
        WalletSpec(
            id="beta",
            label="Beta",
            source_type="hyperliquid_wallet",
            source_ref=ADDRESS_2,
            stage="validation",
            coins=("HYPE", "BTC"),
        )
    )
    assert required_market_coins(registry, ("SOL",)) == ("SOL", "BTC", "ETH", "HYPE")


def test_registry_refuses_more_than_ten_active_hyperliquid_users_per_ip(tmp_path: Path):
    registry = WalletRegistry(tmp_path / "wallets.json")
    for index in range(10):
        registry.add(
            WalletSpec(
                id=f"wallet-{index}",
                label=f"Wallet {index}",
                source_type="hyperliquid_wallet",
                source_ref=f"0x{index + 1:040x}",
                stage="validation",
                coins=("BTC",),
            )
        )
    with pytest.raises(ValueError, match="per-IP"):
        registry.add(
            WalletSpec(
                id="wallet-10",
                label="Wallet 10",
                source_type="hyperliquid_wallet",
                source_ref=f"0x{11:040x}",
                stage="validation",
                coins=("BTC",),
            )
        )


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
