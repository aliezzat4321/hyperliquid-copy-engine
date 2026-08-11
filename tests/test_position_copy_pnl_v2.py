from decimal import Decimal

from hlcopy.copyability.slippage import BookLevel
from hlcopy.profitability.position_copy import CopyFillEvent, simulate_copy
from hlcopy.shadow.evaluator import TapeBook
from hlcopy.shadow.latency import LatencyScenario

D = Decimal


class Provider:
    def __init__(self, books):
        self.books = books

    def first_at_or_after(self, coin, target_ms):
        for book in self.books:
            if book.coin == coin and book.exchange_ts_ms >= target_ms:
                return book
        return None


def book(ts, px):
    p = D(px)
    return TapeBook(
        coin="BTC",
        exchange_ts_ms=ts,
        received_at_ns=ts * 1_000_000,
        bids=(BookLevel(p, D("1000")),),
        asks=(BookLevel(p, D("1000")),),
    )


def event(ts, start, after, tid):
    start = D(start)
    after = D(after)
    return CopyFillEvent(
        lane="DIRECT",
        wallet_id="w1",
        wallet_address="0x" + "1" * 40,
        coin="BTC",
        exchange_ts_ms=ts,
        received_at_ns=ts * 1_000_000,
        tid=tid,
        leader_start=start,
        leader_after=after,
        leader_delta=after - start,
        source_price=D("100"),
    )


def test_flat_open_then_partial_reduce_realizes_pnl():
    sim = simulate_copy(
        [event(1000, "0", "10", 1), event(2000, "10", "5", 2)],
        provider=Provider([book(1000, "100"), book(2000, "110")]),
        scenario=LatencyScenario("TEST", 0, 0, 0),
        notional_usd=D("1000"),
        taker_fee_bps=D("4.5"),
        max_slippage_bps=D("20"),
        max_book_forward_ms=1,
    )
    assert sim.copied_increase_events == 1
    assert len(sim.realized_slices) == 1
    assert sim.realized_gross_pnl_usd == D("50")
    assert sim.total_fees_usd == D("0.69750")
    assert sim.open_positions == 1


def test_legacy_position_increase_copies_only_observed_fraction():
    sim = simulate_copy(
        [event(1000, "90", "100", 1), event(2000, "100", "90", 2)],
        provider=Provider([book(1000, "100"), book(2000, "110")]),
        scenario=LatencyScenario("TEST", 0, 0, 0),
        notional_usd=D("1000"),
        taker_fee_bps=D("0"),
        max_slippage_bps=D("20"),
        max_book_forward_ms=1,
    )
    assert sim.copied_increase_events == 1
    assert len(sim.realized_slices) == 1
    assert sim.realized_slices[0].qty == D("1")
    assert sim.realized_gross_pnl_usd == D("10")
    assert sim.open_positions == 0
