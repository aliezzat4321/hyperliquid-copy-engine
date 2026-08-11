from __future__ import annotations


def canonical_coin(value: object) -> str:
    """Normalize coin identifiers without destroying case-significant native symbols.

    HIP-3 markets use a DEX namespace and are kept in a stable uppercase internal
    form. Ordinary native symbols are uppercase. Hyperliquid also has native
    multiplier symbols such as ``kBONK`` whose leading lowercase ``k`` is part of
    the exchange symbol; preserve that form when it is present in source data/meta.
    """
    text = str(value).strip()
    if not text:
        return ""
    if ":" in text:
        return text.upper()
    if (
        len(text) > 1
        and text[0] == "k"
        and text[1:] == text[1:].upper()
        and any(char.isalpha() for char in text[1:])
    ):
        return text
    return text.upper()


def wire_coin(value: object) -> str:
    """Return the exchange-facing Hyperliquid symbol.

    Native symbols retain their canonical exchange case (including multiplier
    symbols such as ``kBONK``). HIP-3 symbols are namespaced as ``dex:COIN``;
    the DEX namespace on exchange messages/subscriptions is lowercase.
    """
    text = canonical_coin(value)
    if not text or ":" not in text:
        return text
    dex, coin = text.split(":", 1)
    return f"{dex.lower()}:{coin}"
