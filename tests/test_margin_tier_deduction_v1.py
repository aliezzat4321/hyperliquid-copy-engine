from decimal import Decimal

from hlcopy.profitability.path_risk import (
    EquityCheckpoint,
    OpenPositionMark,
    evaluate_cross_margin_path,
)

D = Decimal


def test_maintenance_margin_uses_rate_minus_tier_deduction() -> None:
    position = OpenPositionMark(
        coin="BTC",
        qty=D("10"),
        avg_entry=D("100"),
        mark_price=D("100"),
        maintenance_margin_rate=D("0.05"),
        maintenance_margin_deduction_usd=D("10"),
    )
    assert position.gross_notional_usd == D("1000")
    assert position.maintenance_margin_usd == D("40.00")

    result = evaluate_cross_margin_path(
        [
            EquityCheckpoint(
                exchange_ts_ms=1,
                realized_net_pnl_usd=D("0"),
                funding_pnl_usd=D("0"),
                positions=(position,),
            )
        ],
        starting_equity_usd=D("45"),
        leverage=D("10"),
    )
    assert result.liquidated is False
    assert result.min_liquidation_buffer_usd == D("5.00")


def test_maintenance_margin_never_goes_negative_after_deduction() -> None:
    position = OpenPositionMark(
        coin="X",
        qty=D("1"),
        avg_entry=D("1"),
        mark_price=D("1"),
        maintenance_margin_rate=D("0.01"),
        maintenance_margin_deduction_usd=D("100"),
    )
    assert position.maintenance_margin_usd == D("0")
