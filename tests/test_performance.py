from decimal import Decimal

from hlcopy.analytics.performance import calculate_wallet_metrics
from hlcopy.positions.state_machine import PositionEpisode


def episode(pnl, start, end, complete=True):
    return PositionEpisode(
        wallet_address="0x" + "1" * 40,
        coin="BTC",
        direction="LONG",
        opened_at_ms=start,
        closed_at_ms=end,
        avg_entry=Decimal("100"),
        avg_exit=Decimal("110"),
        max_abs_size=Decimal("1"),
        realized_pnl=Decimal(str(pnl)),
        fees=Decimal("0"),
        complete_start=complete,
        fill_count=2,
    )


def test_metrics_do_not_count_truncated_episode():
    metrics = calculate_wallet_metrics(
        [episode(1000, 0, 1000, complete=False), episode(10, 2000, 5000)], []
    )
    assert metrics.trade_count == 1
    assert metrics.net_pnl_before_funding == 10
    assert metrics.median_hold_seconds == 3
