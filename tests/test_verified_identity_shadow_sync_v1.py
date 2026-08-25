from __future__ import annotations

import json
from pathlib import Path

import pytest

from hlcopy.discovery.verified_identity_shadow_sync import (
    _stable_id,
    sync_verified_identities,
)
from hlcopy.shadow.registry import WalletRegistry, WalletSpec


def _address(value: int) -> str:
    return f"0x{value:040x}"


def _write_identities(
    path: Path,
    rows: list[dict[str, str]],
) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source": "invo",
                "verified_count": len(rows),
                "identities": rows,
            }
        ),
        encoding="utf-8",
    )


def _identity(
    portfolio_id: str,
    wallet: str,
    *,
    username: str = "carmine",
    evidence_sha: str = "evidence-v1",
) -> dict[str, str]:
    return {
        "portfolio_id": portfolio_id,
        "username": username,
        "wallet": wallet,
        "evidence_sha256": evidence_sha,
        "resolver_rule_version": "sqd-public-trade-v3-size-aware-sequence",
        "identified_at": "2026-08-25T00:00:00+00:00",
        "confidence": "0.8",
    }


def test_new_verified_identity_enters_shadow_validation(tmp_path: Path) -> None:
    identities = tmp_path / "identified_wallets.json"
    registry_path = tmp_path / "wallets.json"
    wallet = _address(101)
    _write_identities(identities, [_identity("portfolio-carmine", wallet)])

    result = sync_verified_identities(
        identities_path=identities,
        registry_path=registry_path,
    )

    assert result["outcomes"] == {"added_validation": 1}
    stored = WalletRegistry(registry_path).load()
    assert len(stored) == 1
    assert stored[0].id == _stable_id("portfolio-carmine")
    assert stored[0].source_ref == wallet
    assert stored[0].stage == "validation"
    assert stored[0].enabled is True
    assert stored[0].coins == ()
    assert "source=invo" in stored[0].notes


def test_stale_verified_identity_is_revoked_from_shadow(tmp_path: Path) -> None:
    identities = tmp_path / "identified_wallets.json"
    registry_path = tmp_path / "wallets.json"
    wallet = _address(102)
    _write_identities(identities, [_identity("portfolio-carmine", wallet)])
    sync_verified_identities(identities_path=identities, registry_path=registry_path)

    _write_identities(identities, [])
    result = sync_verified_identities(
        identities_path=identities,
        registry_path=registry_path,
    )

    assert result["revoked_stale"] == 1
    stored = WalletRegistry(registry_path).load()[0]
    assert stored.stage == "research"
    assert stored.enabled is False
    assert "current_verified_identity=false" in stored.notes


def test_corrupt_publication_revokes_managed_wallet_before_failing(tmp_path: Path) -> None:
    identities = tmp_path / "identified_wallets.json"
    registry_path = tmp_path / "wallets.json"
    wallet = _address(103)
    _write_identities(identities, [_identity("portfolio-carmine", wallet)])
    sync_verified_identities(identities_path=identities, registry_path=registry_path)

    identities.write_text("{corrupt-json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid verified identity publication"):
        sync_verified_identities(
            identities_path=identities,
            registry_path=registry_path,
        )

    stored = WalletRegistry(registry_path).load()[0]
    assert stored.stage == "research"
    assert stored.enabled is False
    assert "current_verified_identity=false" in stored.notes


def test_capacity_full_keeps_new_verified_wallet_waiting_in_research(tmp_path: Path) -> None:
    identities = tmp_path / "identified_wallets.json"
    registry_path = tmp_path / "wallets.json"
    registry = WalletRegistry(registry_path)
    registry.init()
    for index in range(1, 11):
        registry.add(
            WalletSpec(
                id=f"manual-{index}",
                label=f"manual-{index}",
                source_type="hyperliquid_wallet",
                source_ref=_address(index),
                stage="validation",
            )
        )

    waiting_wallet = _address(500)
    _write_identities(
        identities,
        [_identity("portfolio-bones", waiting_wallet, username="bones")],
    )
    result = sync_verified_identities(
        identities_path=identities,
        registry_path=registry_path,
    )

    assert result["active_hyperliquid_wallets"] == 10
    assert result["outcomes"] == {"capacity_waiting": 1}
    managed = {
        row.id: row for row in WalletRegistry(registry_path).load()
    }[_stable_id("portfolio-bones")]
    assert managed.stage == "research"
    assert managed.enabled is True
    assert managed.source_ref == waiting_wallet


def test_manual_wallet_collision_is_not_seized_or_mutated(tmp_path: Path) -> None:
    identities = tmp_path / "identified_wallets.json"
    registry_path = tmp_path / "wallets.json"
    wallet = _address(700)
    registry = WalletRegistry(registry_path)
    registry.init()
    registry.add(
        WalletSpec(
            id="manual-wallet",
            label="manual",
            source_type="hyperliquid_wallet",
            source_ref=wallet,
            stage="research",
            enabled=True,
            notes="operator-owned",
        )
    )
    _write_identities(identities, [_identity("portfolio-carmine", wallet)])

    result = sync_verified_identities(
        identities_path=identities,
        registry_path=registry_path,
    )

    assert result["outcomes"] == {"external_registry_conflict": 1}
    stored = WalletRegistry(registry_path).load()
    assert len(stored) == 1
    assert stored[0].id == "manual-wallet"
    assert stored[0].stage == "research"
    assert stored[0].enabled is True
    assert stored[0].notes == "operator-owned"


def test_changed_wallet_revokes_old_subscription_before_failing(tmp_path: Path) -> None:
    identities = tmp_path / "identified_wallets.json"
    registry_path = tmp_path / "wallets.json"
    first = _address(801)
    second = _address(802)
    _write_identities(identities, [_identity("portfolio-carmine", first)])
    sync_verified_identities(identities_path=identities, registry_path=registry_path)

    _write_identities(
        identities,
        [_identity("portfolio-carmine", second, evidence_sha="evidence-v2")],
    )
    with pytest.raises(ValueError, match="changed wallet"):
        sync_verified_identities(
            identities_path=identities,
            registry_path=registry_path,
        )

    stored = WalletRegistry(registry_path).load()
    assert len(stored) == 1
    assert stored[0].source_ref == first
    assert stored[0].stage == "research"
    assert stored[0].enabled is False
    assert f"wallet_changed_to={second}" in stored[0].notes
