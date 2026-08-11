from hlcopy.hyperliquid.websocket import _market_subscriptions
from hlcopy.market.symbols import canonical_coin, wire_coin
from hlcopy.shadow.registry import WalletSpec


def test_internal_and_wire_coin_forms_are_distinct_for_hip3() -> None:
    assert canonical_coin("xyz:SNDK") == "XYZ:SNDK"
    assert canonical_coin("XYZ:sndk") == "XYZ:SNDK"
    assert wire_coin("XYZ:SNDK") == "xyz:SNDK"
    assert wire_coin("btc") == "BTC"


def test_market_subscriptions_emit_lowercase_hip3_namespace() -> None:
    subscriptions = _market_subscriptions(("BTC", "XYZ:SNDK"))
    assert len(subscriptions) == 8
    assert {item["coin"] for item in subscriptions} == {"BTC", "xyz:SNDK"}
    assert {item["type"] for item in subscriptions} == {
        "bbo",
        "l2Book",
        "trades",
        "activeAssetCtx",
    }


def test_l2_only_subscription_mode_keeps_hip3_wire_symbol() -> None:
    subscriptions = _market_subscriptions(("BTC", "XYZ:SNDK"), ("l2Book",))
    assert subscriptions == [
        {"type": "l2Book", "coin": "BTC"},
        {"type": "l2Book", "coin": "xyz:SNDK"},
    ]


def test_registry_keeps_stable_internal_form_for_hip3() -> None:
    wallet = WalletSpec(
        id="wallet",
        label="wallet",
        source_type="hyperliquid_wallet",
        source_ref="0x" + "1" * 40,
        stage="validation",
        coins=("xyz:SNDK", "BTC"),
    )
    assert wallet.coins == ("XYZ:SNDK", "BTC")
