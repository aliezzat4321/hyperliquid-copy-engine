from __future__ import annotations

import os

from hlcopy.shadow.registry import WalletRegistry


class TradingPermissionError(RuntimeError):
    pass


def assert_source_trade_allowed(
    *,
    registry: WalletRegistry,
    source_id: str,
    real_trading_enabled: str | None = None,
) -> None:
    """Hard boundary for future execution code; does not place an order."""
    gate = real_trading_enabled
    if gate is None:
        gate = os.getenv("REAL_TRADING_ENABLED", "NO")
    if gate.strip().upper() != "YES":
        raise TradingPermissionError("REAL_TRADING_ENABLED is not YES")

    matches = [wallet for wallet in registry.load() if wallet.id == source_id]
    if len(matches) != 1:
        raise TradingPermissionError(f"source is not uniquely registered: {source_id}")
    source = matches[0]
    if not source.enabled:
        raise TradingPermissionError(f"source is disabled: {source_id}")
    if source.stage != "approved":
        raise TradingPermissionError(
            f"source stage is {source.stage!r}, expected 'approved': {source_id}"
        )
