from decimal import Decimal

from hlcopy.copyability.slippage import BookLevel, estimate_marketable_fill

D = Decimal


def test_buy_walks_asks_and_computes_vwap():
    result = estimate_marketable_fill(
        side="BUY",
        quantity=D("3"),
        levels=[BookLevel(D("100.1"), D("1")), BookLevel(D("100.2"), D("2"))],
        reference_mid=D("100"),
    )
    assert result.complete is True
    assert result.filled_size == D("3")
    assert result.vwap == D("100.1666666666666666666666667")
    assert result.slippage_bps > D("16")


def test_slippage_cap_turns_excess_depth_into_partial_fill():
    result = estimate_marketable_fill(
        side="BUY",
        quantity=D("3"),
        levels=[BookLevel(D("100.05"), D("1")), BookLevel(D("101"), D("10"))],
        reference_mid=D("100"),
        max_slippage_bps=D("10"),
    )
    assert result.complete is False
    assert result.filled_size == D("1")
    assert result.vwap == D("100.05")
