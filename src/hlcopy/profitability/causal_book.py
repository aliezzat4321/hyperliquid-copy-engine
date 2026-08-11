from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from pathlib import Path

from hlcopy.shadow.evaluator import ParquetL2BookProvider, TapeBook


class CausalParquetL2BookProvider(ParquetL2BookProvider):
    """Return the latest L2 book received before a simulated local send time.

    The base parquet provider caches every parsed coin forever. That is convenient for
    small research runs but unsafe on the production tape: a profitability sweep can
    touch hundreds of markets and retain all parsed order books until process exit.
    Keep only a small LRU working set instead. Timestamp indexes are evicted together
    with their books, so memory is bounded by ``max_cached_coins`` rather than by the
    total number of markets encountered during the sweep.
    """

    def __init__(
        self,
        market_dir: Path,
        *,
        max_age_ms: float = 6000.0,
        max_cached_coins: int = 4,
    ) -> None:
        super().__init__(market_dir)
        self.max_age_ms = max(0.0, float(max_age_ms))
        self.max_cached_coins = max(1, int(max_cached_coins))
        self._cache = OrderedDict()
        self._received_ms_cache: dict[str, tuple[float, ...]] = {}

    def _load_coin(self, coin: str) -> list[TapeBook]:
        cached = self._cache.get(coin)
        if cached is not None:
            self._cache.move_to_end(coin)
            return cached

        books = super()._load_coin(coin)
        # super() inserted the coin into our OrderedDict. Mark it most-recently used.
        self._cache.move_to_end(coin)
        while len(self._cache) > self.max_cached_coins:
            evicted_coin, _ = self._cache.popitem(last=False)
            self._received_ms_cache.pop(evicted_coin, None)
        return books

    def _received_ms(self, coin: str, books: list[TapeBook]) -> tuple[float, ...]:
        cached = self._received_ms_cache.get(coin)
        if cached is None:
            cached = tuple(book.received_at_ns / 1_000_000 for book in books)
            self._received_ms_cache[coin] = cached
        return cached

    def first_at_or_after(self, coin: str, target_ms: float) -> TapeBook | None:
        # Keep the historical method name because simulate_copy calls this interface.
        # Causality is based on local receipt time, not exchange publication time.
        books = self._load_coin(coin)
        if not books:
            return None

        received_ms = self._received_ms(coin, books)
        idx = bisect_right(received_ms, target_ms) - 1
        if idx < 0:
            return None

        book = books[idx]
        age_ms = target_ms - received_ms[idx]
        if age_ms < 0 or age_ms > self.max_age_ms:
            return None
        return book
