from decimal import Decimal

from hlcopy.copyability.backtest import BacktestConfig, run_backtest
from hlcopy.copyability.slippage import BookLevel
from hlcopy.market.historical_archive import L2Snapshot
from hlcopy.signals.invo import CopySignal

D = Decimal


def _signal(direction: str = "SHORT") -> CopySignal:
    return CopySignal(
        signal_id="t1",
        source="test",
        trader="bones",
        coin="BTC",
        direction=direction,
        source_leverage=D("40"),
        allocation_fraction=D("0.01"),
        entry_price=D("65183"),
        exit_price=D("65049"),
        opened_at_ms=1_000,
        closed_at_ms=61_000,
        entry_sim=None,
        last_sim=None,
        reason_closed="user_closed",
        liquidated=False,
        raw={},
    )


def test_source_price_baseline_is_fee_adjusted_and_labeled():
    summary, rows = run_backtest(
        [_signal()],
        BacktestConfig(
            starting_capital=D("10000"),
            latency_ms=1000,
            follower_leverage=D("5"),
            taker_fee_rate=D("0.00045"),
        ),
    )
    assert summary.mode == "SOURCE_PRICE_BASELINE"
    assert summary.copied == 1
    assert rows[0].net_pnl > 0
    assert rows[0].fees > 0
    assert summary.ending_capital == D("10000") + rows[0].net_pnl
    assert summary.funding_mode == "NOT_MODELED"
    assert summary.liquidation_path_mode == "NOT_MODELED"


class StaticProvider:
    def snapshot_at_or_before(self, coin: str, timestamp_ms: int):
        if timestamp_ms < 50_000:
            return L2Snapshot(
                coin=coin,
                timestamp_ms=timestamp_ms - 100,
                bids=(BookLevel(D("99.9"), D("100")),),
                asks=(BookLevel(D("100.1"), D("100")),),
            )
        return L2Snapshot(
            coin=coin,
            timestamp_ms=timestamp_ms - 100,
            bids=(BookLevel(D("98.9"), D("100")),),
            asks=(BookLevel(D("99.1"), D("100")),),
        )


def test_l2_replay_uses_executable_book_and_latency_timestamp():
    signal = CopySignal(
        signal_id="l2",
        source="test",
        trader="bones",
        coin="BTC",
        direction="SHORT",
        source_leverage=D("40"),
        allocation_fraction=D("0.01"),
        entry_price=D("100"),
        exit_price=D("99"),
        opened_at_ms=1_000,
        closed_at_ms=61_000,
        entry_sim=None,
        last_sim=None,
        reason_closed="user_closed",
        liquidated=False,
        raw={},
    )
    summary, rows = run_backtest(
        [signal],
        BacktestConfig(
            starting_capital=D("10000"),
            latency_ms=1_000,
            follower_leverage=D("5"),
            taker_fee_rate=D("0"),
            max_slippage_bps=D("20"),
        ),
        book_provider=StaticProvider(),
    )
    row = rows[0]
    assert summary.mode == "L2_EXECUTION"
    assert row.status == "COPIED"
    assert row.entry_timestamp_ms == 1_900
    assert row.exit_timestamp_ms == 61_900
    assert row.entry_vwap == D("99.9")
    assert row.exit_vwap == D("99.1")
    assert row.net_pnl > 0


class ThinProvider(StaticProvider):
    def snapshot_at_or_before(self, coin: str, timestamp_ms: int):
        return L2Snapshot(
            coin=coin,
            timestamp_ms=timestamp_ms,
            bids=(BookLevel(D("99.9"), D("0.001")),),
            asks=(BookLevel(D("100.1"), D("0.001")),),
        )


def test_l2_replay_fails_closed_on_insufficient_entry_depth():
    summary, rows = run_backtest(
        [_signal()],
        BacktestConfig(
            starting_capital=D("10000"),
            follower_leverage=D("5"),
            max_slippage_bps=D("20"),
        ),
        book_provider=ThinProvider(),
    )
    assert summary.copied == 0
    assert summary.missed == 1
    assert rows[0].reason == "ENTRY_DEPTH_OR_SLIPPAGE"


def test_concurrent_margin_reservation_prevents_future_profit_sizing():
    first = CopySignal(
        signal_id="a",
        source="test",
        trader="bones",
        coin="BTC",
        direction="LONG",
        source_leverage=D("10"),
        allocation_fraction=D("0.40"),
        entry_price=D("100"),
        exit_price=D("110"),
        opened_at_ms=1_000,
        closed_at_ms=100_000,
        entry_sim=None,
        last_sim=None,
        reason_closed="user_closed",
        liquidated=False,
        raw={},
    )
    second = CopySignal(
        signal_id="b",
        source="test",
        trader="bones",
        coin="BTC",
        direction="LONG",
        source_leverage=D("10"),
        allocation_fraction=D("0.40"),
        entry_price=D("100"),
        exit_price=D("110"),
        opened_at_ms=2_000,
        closed_at_ms=50_000,
        entry_sim=None,
        last_sim=None,
        reason_closed="user_closed",
        liquidated=False,
        raw={},
    )
    summary, rows = run_backtest(
        [first, second],
        BacktestConfig(
            starting_capital=D("10000"),
            follower_leverage=D("1"),
            taker_fee_rate=D("0"),
            max_margin_fraction_per_trade=D("0.40"),
            max_total_margin_fraction=D("0.50"),
        ),
    )
    by_id = {row.signal_id: row for row in rows}
    assert by_id["a"].margin_reserved == D("4000")
    assert by_id["b"].margin_reserved == D("1000")
    assert summary.max_margin_reserved == D("5000")
