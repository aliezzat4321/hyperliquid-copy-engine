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


def book(ts: int, px: str, depth: str) -> TapeBook:
    p = D(px)
    size = D(depth)
    return TapeBook(
        coin="BTC",
        exchange_ts_ms=ts,
        received_at_ns=ts * 1_000_000,
        bids=(BookLevel(p, size),),
        asks=(BookLevel(p, size),),
    )


def event(ts: int, start: str, after: str, tid: int) -> CopyFillEvent:
    before = D(start)
    result = D(after)
    return CopyFillEvent(
        lane="DIRECT",
        wallet_id="w1",
        wallet_address="0x" + "1" * 40,
        coin="BTC",
        exchange_ts_ms=ts,
        received_at_ns=ts * 1_000_000,
        tid=tid,
        leader_start=before,
        leader_after=result,
        leader_delta=result - before,
        source_price=D("100"),
    )


def test_failed_flip_close_keeps_old_follower_position() -> None:
    sim = simulate_copy_with_portfolio_capital(
        [event(1000, "0", "10", 1), event(2000, "10", "-10", 2)],
        provider=Provider([
            book(1000, "100", "1000"),
            book(2000, "100", "1"),
        ]),
        scenario=LatencyScenario("TEST", 0, 0, 0),
        notional_usd=D("1000"),
        taker_fee_bps=D("4.5"),
        max_slippage_bps=D("20"),
        max_book_forward_ms=1,
    )

    assert sim.open_positions == 1
    assert sim.missed_events == 1
    assert sim.realized_slices == ()
    assert [item.action for item in sim.state_events] == ["INCREASE"]
    assert sim.state_events[-1].qty_after == D("10")
