from decimal import Decimal

from hlcopy.copyability.source_truth import audit_source_truth
from hlcopy.signals.invo import CopySignal

D = Decimal


def _signal(signal_id: str, direction: str, entry: str, exit_: str, leverage: str, size: str):
    return CopySignal(
        signal_id=signal_id,
        source="test",
        trader="bones",
        coin="BTC",
        direction=direction,
        source_leverage=D(leverage),
        allocation_fraction=D(size),
        entry_price=D(entry),
        exit_price=D(exit_),
        opened_at_ms=1,
        closed_at_ms=2,
        entry_sim=D("999999"),
        last_sim=D("1"),
        reason_closed="user_closed",
        liquidated=False,
        raw={},
    )


def test_source_truth_uses_each_trade_size_and_ignores_sim_fields():
    winning_short = _signal("a", "SHORT", "100", "99", "10", "0.01")
    losing_long = _signal("b", "LONG", "100", "99", "20", "0.03")

    truth = audit_source_truth((winning_short, losing_long))

    assert truth.trades == 2
    assert truth.gross_winners == 1
    assert truth.gross_losers == 1
    assert truth.gross_win_rate == D("0.5")
    # +10% on 1% allocation and -20% on 3% allocation = -0.5% portfolio contribution.
    assert truth.weighted_gross_portfolio_return_sum == D("-0.005")
    assert truth.allocation_min == D("0.01")
    assert truth.allocation_max == D("0.03")
