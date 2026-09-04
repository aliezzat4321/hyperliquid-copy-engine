from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict

import polars as pl

from hlcopy.profitability.causal_book import CausalParquetL2BookProvider


class Lane3ForwardBookProvider(CausalParquetL2BookProvider):
    """Bounded first-observation-at-or-after adapter for Lane 3 decisions.

    The shared provider preserves its established last-at-or-before behavior for its
    existing replay callers. Lane 3 explicitly needs the next captured observation.
    """

    def _forward_candidates(self, coin: str, targets: list[int], age_ns: int) -> list[int]:
        received: set[int] = set()
        by_date: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for target in targets:
            end = target + age_ns
            by_date[self._date_for_ns(target)].append((target, end))
            end_date = self._date_for_ns(end)
            if end_date != self._date_for_ns(target):
                by_date[end_date].append((target, end))
        for date, windows in by_date.items():
            source = self._partition_glob(date, coin)
            if source is None:
                continue
            frame = (
                pl.scan_parquet(source)
                .filter(self._window_expr(windows))
                .select("received_at_ns")
                .collect(engine="streaming")
            )
            self.prime_candidate_rows += frame.height
            self.prime_peak_candidate_rows = max(
                self.prime_peak_candidate_rows, frame.height
            )
            received.update(int(value) for value in frame.get_column("received_at_ns"))
        return sorted(received)

    def _resolve_forward(self, coin: str, target: int) -> None:
        age_ns = int(self.max_age_ms * 1_000_000)
        received = self._forward_candidates(coin, [target], age_ns)
        index = bisect_left(received, target)
        if index >= len(received) or received[index] - target > age_ns:
            self._targeted[(coin, target)] = None
            return
        selected = received[index]
        self._targeted[(coin, target)] = self._selected_books(coin, {selected}).get(selected)

    def first_at_or_after(self, coin: str, target_ms: float):
        target = self._target_ns(target_ms)
        key = (coin, target)
        if key not in self._targeted:
            self._resolve_forward(coin, target)
        return self._targeted[key]
