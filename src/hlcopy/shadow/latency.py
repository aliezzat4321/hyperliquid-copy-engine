from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LatencyScenario:
    """Execution latency budget with feed and order-path latency separated.

    Values must come from measurement or an explicitly named stress scenario. This module deliberately
    ships no guessed production defaults.
    """

    name: str
    decision_ms: float
    outbound_order_ms: float
    exchange_processing_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("latency scenario name is required")
        for field_name, value in (
            ("decision_ms", self.decision_ms),
            ("outbound_order_ms", self.outbound_order_ms),
            ("exchange_processing_ms", self.exchange_processing_ms),
        ):
            if value < 0:
                raise ValueError(f"{field_name} cannot be negative")

    @property
    def post_receipt_ms(self) -> float:
        return self.decision_ms + self.outbound_order_ms + self.exchange_processing_ms


@dataclass(frozen=True, slots=True)
class ObservedSignalLatency:
    exchange_ts_ms: int
    local_received_at_ns: int

    @property
    def feed_ms(self) -> float:
        return self.local_received_at_ns / 1_000_000 - self.exchange_ts_ms

    def clock_plausible(self, *, min_ms: float = -100.0, max_ms: float = 10_000.0) -> bool:
        return min_ms <= self.feed_ms <= max_ms

    def estimated_order_arrival_ms(self, scenario: LatencyScenario) -> float:
        """Exchange-clock target for a follower order under an explicit latency budget."""
        if not self.clock_plausible():
            raise ValueError(f"implausible exchange/local clock delta: {self.feed_ms:.3f} ms")
        return self.exchange_ts_ms + max(0.0, self.feed_ms) + scenario.post_receipt_ms
