from decimal import Decimal

import pytest

from hlcopy.profitability.path_risk import (
    EquityCheckpoint,
    OpenPositionMark,
    evaluate_cross_margin_path,
)

D = Decimal


def position(*, qty: str, entry: str, mark: str, mmr: str = "0.05") -> OpenPositionMark:
    return OpenPositionMark(
        coin="BTC",
        qty=D(qty),
        avg_entry=D(entry),
        mark_price=D(mark),
        maintenance_margin_rate=D(mmr),
    )


def checkpoint(
    ts: int,
    *,
    qty: str,
    entry: str,
    mark: str,
    realized: str = "0",
    funding: str = "0",
    mmr: str = "0.05",
) -> EquityCheckpoint:
    return EquityCheckpoint(
        exchange_ts_ms=ts,
        realized_net_pnl_usd=D(realized),
        funding_pnl_usd=D(funding),
        positions=(position(qty=qty, entry=entry, mark=mark, mmr=mmr),),
    )


def test_long_mtm_drawdown_is_measured_from_equity_peak() -> None:
    result = evaluate_cross_margin_path(
        [
            checkpoint(1, qty="1", entry="100", mark="120"),
            checkpoint(2, qty="1", entry="100", mark="90"),
        ],
        starting_equity_usd=D("100"),
        leverage=D("5"),
    )

    assert result.max_equity_usd == D("120")
    assert result.min_equity_usd == D("90")
    assert result.max_drawdown_usd == D("30")
    assert result.max_drawdown_pct == D("25.00")
    assert result.liquidated is False


def test_signed_qty_marks_short_positions_correctly() -> None:
    result = evaluate_cross_margin_path(
        [checkpoint(1, qty="-2", entry="100", mark="90")],
        starting_equity_usd=D("100"),
        leverage=D("5"),
    )

    point = result.checkpoints[0]
    assert point.unrealized_pnl_usd == D("20")
    assert point.equity_usd == D("120")
    assert point.gross_notional_usd == D("180")


def test_funding_can_push_account_through_maintenance_margin() -> None:
    result = evaluate_cross_margin_path(
        [
            checkpoint(
                123,
                qty="10",
                entry="100",
                mark="100",
                funding="-60",
                mmr="0.05",
            )
        ],
        starting_equity_usd=D("100"),
        leverage=D("10"),
    )

    point = result.checkpoints[0]
    assert point.equity_usd == D("40")
    assert point.maintenance_margin_usd == D("50.00")
    assert point.liquidation_buffer_usd == D("-10.00")
    assert result.liquidated is True
    assert result.first_liquidation_ts_ms == 123


def test_initial_margin_and_free_collateral_reflect_requested_leverage() -> None:
    result = evaluate_cross_margin_path(
        [checkpoint(1, qty="2", entry="100", mark="100")],
        starting_equity_usd=D("100"),
        leverage=D("4"),
    )

    point = result.checkpoints[0]
    assert point.initial_margin_usd == D("50")
    assert point.free_collateral_usd == D("50")


def test_invalid_or_empty_paths_fail_closed() -> None:
    with pytest.raises(ValueError):
        evaluate_cross_margin_path([], starting_equity_usd=D("100"), leverage=D("5"))
    with pytest.raises(ValueError):
        evaluate_cross_margin_path(
            [checkpoint(1, qty="1", entry="100", mark="100")],
            starting_equity_usd=D("0"),
            leverage=D("5"),
        )
    with pytest.raises(ValueError):
        evaluate_cross_margin_path(
            [checkpoint(1, qty="1", entry="100", mark="100")],
            starting_equity_usd=D("100"),
            leverage=D("0"),
        )
