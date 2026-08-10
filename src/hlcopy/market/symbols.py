from __future__ import annotations


def canonical_coin(value: object) -> str:
    """Normalize Hyperliquid coin identifiers without corrupting HIP-3 DEX prefixes.

    Native perp symbols are conventionally uppercase (for example ``BTC``), while
    HIP-3 symbols are namespaced as ``dex:COIN``.  The namespace is part of the
    exchange identifier and must not be uppercased; doing so can turn a valid
    ``xyz:SNDK`` subscription into the invalid ``XYZ:SNDK``.
    """
    text = str(value).strip()
    if not text:
        return ""
    if ":" not in text:
        return text.upper()
    dex, coin = text.split(":", 1)
    return f"{dex.lower()}:{coin.upper()}"
