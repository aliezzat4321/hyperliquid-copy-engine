from decimal import Decimal

from hlcopy.copyability.percent_replay import replay_trade, summarize
from hlcopy.copyability.slippage import BookLevel
from hlcopy.market.historical_archive import L2Snapshot
from hlcopy.signals.invo import CopySignal

D = Decimal


def _signal(*, liquidated: bool = False) -> CopySignal:
    return CopySignal(
        signal_id="bones-btc",
        source="test",
        trader="bones",
        coin="BTC",
        direction="SHORT",
        source_leverage=D("40"),
        allocation_fraction=D("0.01"),
        entry_price=D("65183"),
        exit_price=D("65049"),
        opened_at_ms=1_000,
        closed_at_ms=61_000,
        entry_sim=None,
        last_sim=None,
        reason_closed="liquidated" if liquidated else "user_closed",
        liquidated=liquidated,
        raw={},
    )


def test_source_price_percent_replay_uses_source_leverage_and_no_portfolio_balance():
    signal = _signal()
    row = replay_trade(
        signal,
        latency_ms=0,
        margin_usd=D("100"),
        taker_fee_rate=D("0.00045"),
        max_slippage_bps=D("20"),
        provider=None,
    )

    assert row.status == "EXECUTED"
    assert row.source_leverage == D("40")
    assert row.gross_return_pct == signal.source_leveraged_return * D("100")
    assert row.net_return_pct is not None
    assert row.net_return_pct < row.gross_return_pct
    assert row.net_return_pct > D("0")


class StaticProvider:
    def snapshot_at_or_before(self, coin: str, timestamp_ms: int):
        if timestamp_ms < 50_000:
            return L2Snapshot(
                coin=coin,
                timestamp_ms=timestamp_ms - 100,
                bids=(BookLevel(D("99.9"), D("1000")),),
                asks=(BookLevel(D("100.1"), D("1000")),),
            )
        return L2Snapshot(
            coin=coin,
            timestamp_ms=timestamp_ms - 100,
            bids=(BookLevel(D("98.9"), D("1000")),),
            asks=(BookLevel(D("99.1"), D("1000")),),
        )


def test_l2_percent_replay_uses_delayed_executable_prices():
    signal = CopySignal(
        signal_id="short",
        source="test",
        trader="bones",
        coin="BTC",
        direction="SHORT",
        source_leverage=D("10"),
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
    row = replay_trade(
        signal,
        latency_ms=1_000,
        margin_usd=D("100"),
        taker_fee_rate=D("0"),
        max_slippage_bps=D("20"),
        provider=StaticProvider(),
    )

    assert row.status == "EXECUTED"
    assert row.entry_vwap == D("99.9")
    assert row.exit_vwap == D("99.1")
    assert row.entry_book_age_ms == 100
    assert row.exit_book_age_ms == 100
    assert row.gross_return_pct is not None
    assert row.gross_return_pct > D("0")


class ThinProvider(StaticProvider):
    def snapshot_at_or_before(self, coin: str, timestamp_ms: int):
        return L2Snapshot(
            coin=coin,
            timestamp_ms=timestamp_ms,
            bids=(BookLevel(D("99.9"), D("0.001")),),
            asks=(BookLevel(D("100.1"), D("0.001")),),
        )


def test_percent_replay_fails_closed_when_depth_is_insufficient():
    row = replay_trade(
        _signal(),
        latency_ms=1_000,
        margin_usd=D("1000"),
        taker_fee_rate=D("0.00045"),
        max_slippage_bps=D("20"),
        provider=ThinProvider(),
    )
    assert row.status == "MISSED"
    assert row.reason == "ENTRY_DEPTH_OR_SLIPPAGE"
    assert row.net_return_pct is None


def test_summary_keeps_source_and_follower_win_rates_separate():
    signal = _signal(liquidated=True)
    row = replay_trade(
        signal,
        latency_ms=0,
        margin_usd=D("100"),
        taker_fee_rate=D("0.00045"),
        max_slippage_bps=D("20"),
        provider=None,
    )
    summary = summarize(
        (signal,),
        [row],
        mode="SOURCE_PRICE_BASELINE",
        latency_ms=0,
        margin_usd=D("100"),
    )
    assert summary.source_win_rate == D("1")
    assert summary.follower_net_win_rate == D("1")
    assert summary.source_liquidations == 1
    assert summary.liquidation_path_mode == "NOT_MODELED"
