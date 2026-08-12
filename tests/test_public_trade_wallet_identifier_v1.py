from decimal import Decimal

from hlcopy.resolver.public_trade_index import _public_trade_matches
from hlcopy.signals.invo import CopySignal

D = Decimal


def _signal(direction: str) -> CopySignal:
    return CopySignal(
        signal_id="s1",
        source="invo_export",
        trader="alice",
        coin="BTC",
        direction=direction,
        source_leverage=D("2"),
        allocation_fraction=D("0.25"),
        entry_price=D("100"),
        exit_price=D("110"),
        opened_at_ms=1_000_000,
        closed_at_ms=2_000_000,
        entry_sim=None,
        last_sim=None,
        reason_closed="",
        liquidated=False,
        raw={},
    )


def _row() -> dict[str, object]:
    return {
        "coin": "BTC",
        "time": "1970-01-01T00:33:20+00:00",
        "px": "110.01",
        "hash": "0xabc",
        "side_info": [
            {"user": "0x1111111111111111111111111111111111111111"},
            {"user": "0x2222222222222222222222222222222222222222"},
        ],
    }


def test_long_close_selects_seller_address() -> None:
    matches = _public_trade_matches(
        _signal("LONG"),
        [_row()],
        window_ms=5_000,
        max_price_bps=D("5"),
    )
    assert set(matches) == {"0x2222222222222222222222222222222222222222"}


def test_short_close_selects_buyer_address() -> None:
    matches = _public_trade_matches(
        _signal("SHORT"),
        [_row()],
        window_ms=5_000,
        max_price_bps=D("5"),
    )
    assert set(matches) == {"0x1111111111111111111111111111111111111111"}


def test_wrong_price_does_not_create_candidate() -> None:
    row = _row()
    row["px"] = "120"
    matches = _public_trade_matches(
        _signal("LONG"),
        [row],
        window_ms=5_000,
        max_price_bps=D("5"),
    )
    assert matches == {}
