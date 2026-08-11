from __future__ import annotations

from bisect import bisect_right
from pathlib import Path

from hlcopy.shadow.evaluator import ParquetL2BookProvider, TapeBook


class CausalParquetL2BookProvider(ParquetL2BookProvider):
    """Return the latest L2 book actually received before a simulated local send time.

    ``target_ms`` is treated as local wall-clock milliseconds. The provider rejects
    books older than ``max_age_ms`` based on local receipt time, preventing both
    look-ahead and use of arbitrarily stale market state.
    """

    def __init__(self, market_dir: Path, *, max_age_ms: float = 6000.0) -> None:
        super().__init__(market_dir)
        self.max_age_ms = max(0.0, float(max_age_ms))

    def first_at_or_after(self, coin: str, target_ms: float) -> TapeBook | None:
        # Kept under the existing method name so the position-copy simulator can use
        # this provider without changing the older markout/research provider semantics.
        books = self._load_coin(coin)
        if not books:
            return None

        received_ms = [book.received_at_ns / 1_000_000 for book in books]
        idx = bisect_right(received_ms, target_ms) - 1
        if idx < 0:
            return None

        book = books[idx]
        age_ms = target_ms - (book.received_at_ns / 1_000_000)
        if age_ms < 0 or age_ms > self.max_age_ms:
            return None
        return book
