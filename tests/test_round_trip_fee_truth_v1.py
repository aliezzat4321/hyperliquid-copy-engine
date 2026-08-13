from decimal import Decimal

from hlcopy.copyability.slippage import BookLevel
from hlcopy.profitability.portfolio_position_copy import simulate_copy_with_portfolio_capital
from hlcopy.profitability.position_copy import CopyFillEvent
from hlcopy.shadow.evaluator import TapeBook
from hlcopy.shadow.latency import LatencyScenario

D = Decimal


class Provider:
    def __init__(self, books):
        self.books = books

    def first_at_or_after(self, coin, target_ms):
        for item in self.books:
            if item.coin == coin and item.exchange_ts_ms >= target_ms:
                return item
        return None


def book(ts: int, px: str) -> TapeBook:
    p = D(px)
    return TapeBook(
        coin="BTC",
        exchange_ts_ms=ts,
        received_at_ns=ts * 1_000_000,
        bids=(BookLevel(p, D("1000")),),
        asks=(BookLevel(p, D("1000")),),
    )


def event(ts: int, start: str, after: str, tid: int) -> CopyFillEvent:
    before = D(start)
    end = D(after)
    return CopyFillEvent(
        lane="WIDE",
        wallet_id="w1",
        wallet_address="0x" + "1" * 40,
        coin="BTC",
        exchange_ts_ms=ts,
        received_at_ns=ts * 1_000_000,
        tid=tid,
        leader_start=before,
        leader_after=end,
        leader_delta=end - before,
        source_price=D("100"),
    )


def run(events, books):
    return simulate_copy_with_portfolio_capital(
        events,
        provider=Provider(books),
        scenario=LatencyScenario("TEST", 0, 0, 0),
        notional_usd=D("1000"),
        taker_fee_bps=D("4.5"),
        max_slippage_bps=D("20"),
        max_book_forward_ms=1,
    )


def test_full_close_slice_includes_entry_and_exit_fees() -> None:
    sim = run(
        [event(1000, "0", "10", 1), event(2000, "10", "0", 2)],
        [book(1000, "100"), book(2000, "110")],
    )

    assert len(sim.realized_slices) == 1
    item = sim.realized_slices[0]
    assert item.gross_pnl_usd == D("100")
    assert item.entry_fee_usd_allocated == D("0.45000")
    assert item.fee_usd == D("0.49500")
    assert item.net_pnl_usd == D("99.05500")
    assert sim.realized_net_pnl_usd == D("99.05500")
    assert sim.total_fees_usd == D("0.94500")
    assert sim.realized_gross_pnl_usd - sim.total_fees_usd == D("99.05500")


def test_partial_reductions_allocate_entry_fee_pro_rata_without_double_counting() -> None:
    sim = run(
        [
            event(1000, "0", "10", 1),
            event(2000, "10", "5", 2),
            event(3000, "5", "0", 3),
        ],
        [book(1000, "100"), book(2000, "110"), book(3000, "120")],
    )

    assert len(sim.realized_slices) == 2
    first, second = sim.realized_slices
    assert first.entry_fee_usd_allocated == D("0.225000")
    assert second.entry_fee_usd_allocated == D("0.225000")
    assert first.entry_fee_usd_allocated + second.entry_fee_usd_allocated == D("0.450000")
    assert sum((x.net_pnl_usd for x in sim.realized_slices), D("0")) == sim.realized_net_pnl_usd
    assert sim.realized_net_pnl_usd == sim.realized_gross_pnl_usd - sim.total_fees_usd


def test_flip_allocates_old_entry_fee_then_tracks_new_side_separately() -> None:
    sim = run(
        [
            event(1000, "0", "10", 1),
            event(2000, "10", "-10", 2),
            event(3000, "-10", "0", 3),
        ],
        [book(1000, "100"), book(2000, "110"), book(3000, "100")],
    )

    assert [x.action for x in sim.realized_slices] == ["FLIP_CLOSE", "CLOSE"]
    assert sim.realized_slices[0].entry_fee_usd_allocated == D("0.45000")
    assert sim.realized_slices[1].entry_fee_usd_allocated > D("0")
    assert sim.realized_net_pnl_usd == sim.realized_gross_pnl_usd - sim.total_fees_usd
