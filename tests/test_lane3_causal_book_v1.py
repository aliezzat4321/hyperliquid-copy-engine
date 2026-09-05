from decimal import Decimal as D

import pytest

from hlcopy.copyability.slippage import BookLevel
from hlcopy.lane3.costs import CostCompleteness, measure_leg, position_economics
from hlcopy.lane3.reconstruction import ExecutionLeg, ReconstructedPosition
from hlcopy.shadow.evaluator import TapeBook


class BoundaryProvider:
    def __init__(self, books: list[TapeBook]) -> None:
        self.books = books
        self.requested_arrival_ms: float | None = None

    def at_or_before(self, coin: str, arrival_ms: float) -> TapeBook | None:
        self.requested_arrival_ms = arrival_ms
        eligible = [book for book in self.books if book.received_at_ns <= arrival_ms * 1_000_000]
        return max(eligible, key=lambda book: book.received_at_ns, default=None)


def _book(received_ms: int, bid: str, ask: str) -> TapeBook:
    return TapeBook(
        coin="ETH",
        exchange_ts_ms=received_ms,
        received_at_ns=received_ms * 1_000_000,
        bids=(BookLevel(D(bid), D("10")),),
        asks=(BookLevel(D(ask), D("10")),),
    )


def _position() -> tuple[ReconstructedPosition, ExecutionLeg]:
    leg = ExecutionLeg(1_000, D("100"), D("1"), D("100"), "ENTRY")
    return ReconstructedPosition("p", "trader", "ETH", "long", [leg]), leg


def _measure(provider: BoundaryProvider):
    position, leg = _position()
    return measure_leg(
        position,
        leg,
        provider,
        taker_rate=D("0.00045"),
        max_slippage_bps=D("1000"),
        follower_submit_latency_ms=25,
        transport_latency_ms=75,
    )


@pytest.mark.parametrize("future_bid,future_ask", [("89", "90"), ("110", "111")])
def test_future_jump_inside_book_window_cannot_create_measured_net(
    future_bid: str, future_ask: str
) -> None:
    # Favorable and adverse moves are both after the frozen 1,100 ms arrival.
    cost = _measure(BoundaryProvider([_book(1_500, future_bid, future_ask)]))
    position, leg = _position()
    exit_leg = ExecutionLeg(2_000, D("101"), D("1"), D("101"), "EXIT")
    position.exit_leg = exit_leg
    economics = position_economics(
        position,
        [cost, cost],
        funding_usd=D("0"),
        funding_measured=True,
    )

    assert cost.completeness == CostCompleteness.UNMEASURED_NO_BOOK
    assert cost.crossing_usd is None
    assert economics.net_pnl_usd is None
    assert economics.cost_completeness == CostCompleteness.UNMEASURED_NO_BOOK


@pytest.mark.parametrize("future_bid,future_ask", [("89", "90"), ("110", "111")])
def test_future_jump_cannot_change_causal_arrival_cost(
    future_bid: str, future_ask: str
) -> None:
    causal = _book(1_100, "99", "101")
    baseline = _measure(BoundaryProvider([causal]))
    with_future = _measure(
        BoundaryProvider([causal, _book(1_500, future_bid, future_ask)])
    )

    assert baseline.completeness == CostCompleteness.MEASURED
    assert with_future == baseline
    assert with_future.arrival_timestamp_ms == 1_100
    assert with_future.book_received_at_ns == 1_100_000_000
    assert with_future.evidence_basis == "CAUSAL_SIMULATED_ORDER_ARRIVAL"


def test_price_move_before_arrival_is_fully_charged_as_crossing() -> None:
    cost = _measure(BoundaryProvider([_book(1_100, "101", "102")]))

    assert cost.completeness == CostCompleteness.MEASURED
    assert cost.crossing_bps == D("200")
    assert cost.crossing_usd == D("2")


def test_provider_contract_violation_fails_closed() -> None:
    class BadProvider:
        def at_or_before(self, coin: str, arrival_ms: float) -> TapeBook:
            return _book(1_101, "99", "101")

    position, leg = _position()
    cost = measure_leg(
        position,
        leg,
        BadProvider(),
        taker_rate=D("0.00045"),
        max_slippage_bps=D("1000"),
        follower_submit_latency_ms=25,
        transport_latency_ms=75,
    )

    assert cost.completeness == CostCompleteness.UNMEASURED_NO_BOOK
    assert cost.book_received_at_ns is None


def test_negative_latency_is_rejected() -> None:
    position, leg = _position()
    with pytest.raises(ValueError, match="cannot be negative"):
        measure_leg(
            position,
            leg,
            BoundaryProvider([]),
            taker_rate=D("0.00045"),
            max_slippage_bps=D("1000"),
            follower_submit_latency_ms=-1,
            transport_latency_ms=0,
        )
