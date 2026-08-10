from __future__ import annotations

import json
from pathlib import Path

from hlcopy.shadow.manifest import fingerprint, write_run_manifest
from hlcopy.shadow.registry import WalletRegistry, WalletSpec

ADDRESS = "0x1111111111111111111111111111111111111111"


def test_fingerprint_is_order_stable_for_mapping_keys():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


def test_shadow_manifest_persists_registry_snapshot(tmp_path: Path, monkeypatch):
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
    monkeypatch.setenv("REAL_TRADING_ENABLED", "NO")
    monkeypatch.setenv("HLCOPY_GIT_COMMIT", "deadbeef")

    path = write_run_manifest(
        registry=registry,
        shadow_dir=tmp_path / "shadow",
        websocket_url="wss://api.hyperliquid.xyz/ws",
        extra_coins=("SOL",),
        initial_market_coins=("SOL", "BTC", "ETH"),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["evidence_mode"] == "PROSPECTIVE_LIVE_SHADOW"
    assert payload["real_trading_enabled"] == "NO"
    assert payload["git_commit"] == "deadbeef"
    assert payload["registry_snapshot"][0]["id"] == "alpha"
    assert payload["initial_market_coins"] == ["SOL", "BTC", "ETH"]
