from decimal import Decimal
from pathlib import Path

from hlcopy.copyability.slippage import BookLevel
from hlcopy.profitability.causal_book import CausalParquetL2BookProvider
from hlcopy.shadow.evaluator import TapeBook


def _book(ts_ms: int, received_ms: int) -> TapeBook:
    return TapeBook(
        coin="ETH",
        exchange_ts_ms=ts_ms,
        received_at_ns=received_ms * 1_000_000,
        bids=(BookLevel(Decimal("100"), Decimal("10")),),
        asks=(BookLevel(Decimal("101"), Decimal("10")),),
    )


def test_causal_provider_uses_latest_received_book_without_lookahead() -> None:
    provider = CausalParquetL2BookProvider(Path("/unused"), max_age_ms=6000)
    provider._cache["ETH"] = [
        _book(1000, 1100),
        _book(5000, 5100),
        _book(9000, 9100),
    ]

    chosen = provider.first_at_or_after("ETH", 7000)

    assert chosen is not None
    assert chosen.exchange_ts_ms == 5000
    assert chosen.received_at_ns == 5_100_000_000


def test_causal_provider_rejects_stale_book() -> None:
    provider = CausalParquetL2BookProvider(Path("/unused"), max_age_ms=1000)
    provider._cache["ETH"] = [_book(1000, 1100)]

    assert provider.first_at_or_after("ETH", 2500) is None
