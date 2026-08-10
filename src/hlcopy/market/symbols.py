from __future__ import annotations


def canonical_coin(value: object) -> str:
    """Normalize coin identifiers for internal comparisons and registry storage."""
    return str(value).strip().upper()


def wire_coin(value: object) -> str:
    """Return the exchange-facing Hyperliquid symbol.

    Native perp symbols are uppercase. HIP-3 symbols are namespaced as
    ``dex:COIN``; the DEX namespace on exchange messages/subscriptions is
    lowercase (for example ``xyz:SNDK``), even though the engine keeps an
    uppercase internal representation for stable comparisons.
    """
    text = canonical_coin(value)
    if not text or ":" not in text:
        return text
    dex, coin = text.split(":", 1)
    return f"{dex.lower()}:{coin}"
