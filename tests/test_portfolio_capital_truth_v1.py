from decimal import Decimal

from hlcopy.copyability.slippage import BookLevel
from hlcopy.profitability.leverage_truth import leverage_matrix
from hlcopy.profitability.portfolio_position_copy import simulate_copy_with_portfolio_capital
from hlcopy.profitability.position_copy import CopyFillEvent, simulate_copy
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


def book(coin: str, ts: int, px: str) -> TapeBook:
    p = D(px)
    return TapeBook(
        coin=coin,
        exchange_ts_ms=ts,
        received_at_ns=ts * 1_000_000,
        bids=(BookLevel(p, D("1000")),),
        asks=(BookLevel(p, D("1000")),),
    )


def event(coin: str, ts: int, start: str, after: str, tid: int) -> CopyFillEvent:
    leader_start = D(start)
    leader_after = D(after)
    return CopyFillEvent(
        lane="WIDE",
        wallet_id="w1",
        wallet_address="0x" + "1" * 40,
        coin=coin,
        exchange_ts_ms=ts,
        received_at_ns=ts * 1_000_000,
        tid=tid,
        leader_start=leader_start,
        leader_after=leader_after,
        leader_delta=leader_after - leader_start,
        source_price=D("100"),
    )


def test_portfolio_simulator_preserves_single_coin_execution_pnl() -> None:
    events = [
        event("BTC", 1000, "0", "1", 1),
        event("BTC", 1200, "1", "0", 2),
    ]
    books = [book("BTC", 1000, "100"), book("BTC", 1200, "110")]
    kwargs = {
        "scenario": LatencyScenario("TEST", 0, 0, 0),
        "notional_usd": D("1000"),
        "taker_fee_bps": D("4.5"),
        "max_slippage_bps": D("20"),
        "max_book_forward_ms": 1,
    }
    old = simulate_copy(events, provider=Provider(books), **kwargs)
    new = simulate_copy_with_portfolio_capital(
        events,
        provider=Provider(books),
        **kwargs,
    )
    assert new.realized_gross_pnl_usd == old.realized_gross_pnl_usd
    assert new.total_fees_usd == old.total_fees_usd
    assert new.executable_events == old.executable_events
    assert len(new.realized_slices) == len(old.realized_slices)


def test_two_overlapping_coin_positions_require_portfolio_capital() -> None:
    sim = simulate_copy_with_portfolio_capital(
        [
            event("BTC", 1000, "0", "1", 1),
            event("ETH", 1100, "0", "1", 2),
            event("BTC", 1200, "1", "0", 3),
            event("ETH", 1300, "1", "0", 4),
        ],
        provider=Provider(
            [
                book("BTC", 1000, "100"),
                book("ETH", 1100, "200"),
                book("BTC", 1200, "110"),
                book("ETH", 1300, "220"),
            ]
        ),
        scenario=LatencyScenario("TEST", 0, 0, 0),
        notional_usd=D("1000"),
        taker_fee_bps=D("0"),
        max_slippage_bps=D("20"),
        max_book_forward_ms=1,
    )

    # At BTC's close event, both positions are still open and BTC has marked from
    # $1,000 to $1,100, so causal concurrent gross peaks at $2,100 rather than the
    # single-coin $1,000 cap.
    assert sim.peak_concurrent_gross_notional_usd == D("2100")
    assert sim.realized_gross_pnl_usd == D("200")

    summary = {
        "notional_usd": "1000",
        "peak_concurrent_gross_notional_usd": str(
            sim.peak_concurrent_gross_notional_usd
        ),
        "closed_net_pnl_usd": str(sim.realized_gross_pnl_usd),
        "realized_actions": len(sim.realized_slices),
    }
    row = leverage_matrix(summary, [D("5")])[0]
    assert D(str(row["equity_required_usd"])) == D("420")
    assert D(str(row["net_equity_return_pct"])) == D("1000") / D("21")
