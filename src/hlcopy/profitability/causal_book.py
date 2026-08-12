from __future__ import annotations

import json
from bisect import bisect_right
from collections import OrderedDict, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

from hlcopy.copyability.slippage import BookLevel
from hlcopy.market.symbols import wire_coin
from hlcopy.shadow.evaluator import ParquetL2BookProvider, TapeBook
from hlcopy.shadow.latency import LatencyScenario, ObservedSignalLatency

D = Decimal
ZERO = D("0")


class CausalParquetL2BookProvider(ParquetL2BookProvider):
    """Causal L2 provider optimized for event-targeted profitability sweeps.

    Production profitability only needs the latest book available at a finite set of
    simulated order-arrival timestamps. Priming scans only narrow causal windows around
    those targets and materializes only the selected L2 snapshots.
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
        self._targeted: dict[tuple[str, int], TapeBook | None] = {}
        self.prime_candidate_rows = 0
        self.prime_book_rows = 0

    @staticmethod
    def _target_ns(target_ms: float) -> int:
        return int(round(float(target_ms) * 1_000_000))

    @staticmethod
    def _date_for_ns(value_ns: int) -> str:
        return datetime.fromtimestamp(value_ns / 1_000_000_000, UTC).date().isoformat()

    @staticmethod
    def _merge_windows(targets: Iterable[int], max_age_ns: int) -> list[tuple[int, int]]:
        windows: list[tuple[int, int]] = []
        for target in sorted(set(targets)):
            start = target - max_age_ns
            end = target
            if windows and start <= windows[-1][1] + 1:
                windows[-1] = (windows[-1][0], max(windows[-1][1], end))
            else:
                windows.append((start, end))
        return windows

    @classmethod
    def _windows_by_date(
        cls,
        targets: Iterable[int],
        max_age_ns: int,
    ) -> dict[str, list[tuple[int, int]]]:
        grouped: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for start, end in cls._merge_windows(targets, max_age_ns):
            grouped[cls._date_for_ns(start)].append((start, end))
            end_date = cls._date_for_ns(end)
            if end_date != cls._date_for_ns(start):
                grouped[end_date].append((start, end))
        return grouped

    def _partition_glob(self, date: str, coin: str) -> str | None:
        folder = self.market_dir / f"date={date}" / f"coin={coin}" / "channel=l2Book"
        if not folder.exists() or next(folder.glob("*.parquet"), None) is None:
            return None
        return str(folder / "*.parquet")

    @staticmethod
    def _window_expr(windows: Iterable[tuple[int, int]]) -> pl.Expr:
        expr: pl.Expr | None = None
        for start, end in windows:
            current = pl.col("received_at_ns").is_between(start, end, closed="both")
            expr = current if expr is None else (expr | current)
        if expr is None:
            return pl.lit(False)
        return expr

    def _load_coin(self, coin: str) -> list[TapeBook]:
        cached = self._cache.get(coin)
        if cached is not None:
            self._cache.move_to_end(coin)
            return cached

        books = super()._load_coin(coin)
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

    @staticmethod
    def _levels(raw: object) -> tuple[BookLevel, ...]:
        try:
            values = json.loads(str(raw or "[]"))
            return tuple(
                BookLevel(D(str(level["px"])), D(str(level["sz"])))
                for level in values
                if D(str(level.get("px", "0"))) > ZERO
                and D(str(level.get("sz", "0"))) > ZERO
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, ArithmeticError):
            return ()

    def _timestamp_candidates(
        self,
        coin: str,
        targets: list[int],
        max_age_ns: int,
    ) -> tuple[list[int], dict[int, int]]:
        received_to_exchange: dict[int, int] = {}
        windows_by_date = self._windows_by_date(targets, max_age_ns)

        for date, windows in sorted(windows_by_date.items()):
            source = self._partition_glob(date, coin)
            if source is None:
                continue
            frame = (
                pl.scan_parquet(source)
                .filter(self._window_expr(windows))
                .select(["exchange_ts_ms", "received_at_ns"])
                .collect(engine="streaming")
            )
            self.prime_candidate_rows += frame.height
            for exchange_ts_ms, received_at_ns in frame.iter_rows():
                received_to_exchange[int(received_at_ns)] = int(exchange_ts_ms)

        received = sorted(received_to_exchange)
        return received, received_to_exchange

    def _selected_books(
        self,
        coin: str,
        selected_received: set[int],
    ) -> dict[int, TapeBook | None]:
        selected_by_date: dict[str, list[int]] = defaultdict(list)
        for received_ns in selected_received:
            selected_by_date[self._date_for_ns(received_ns)].append(received_ns)

        parsed: dict[int, TapeBook | None] = {}
        columns = [
            "exchange_ts_ms",
            "received_at_ns",
            "bid_levels_json",
            "ask_levels_json",
        ]
        for date, values in sorted(selected_by_date.items()):
            source = self._partition_glob(date, coin)
            if source is None:
                continue
            frame = (
                pl.scan_parquet(source)
                .filter(pl.col("received_at_ns").is_in(sorted(values)))
                .select(columns)
                .collect(engine="streaming")
            )
            self.prime_book_rows += frame.height
            for row in frame.iter_rows(named=True):
                received_ns = int(row["received_at_ns"])
                bids = self._levels(row.get("bid_levels_json"))
                asks = self._levels(row.get("ask_levels_json"))
                parsed[received_ns] = (
                    TapeBook(
                        coin=coin,
                        exchange_ts_ms=int(row["exchange_ts_ms"]),
                        received_at_ns=received_ns,
                        bids=bids,
                        asks=asks,
                    )
                    if bids and asks
                    else None
                )
        return parsed

    def _resolve_targets(self, coin: str, target_ns_values: Iterable[int]) -> None:
        targets = sorted(
            target
            for target in set(target_ns_values)
            if (coin, target) not in self._targeted
        )
        if not targets:
            return

        max_age_ns = int(self.max_age_ms * 1_000_000)
        received, _ = self._timestamp_candidates(coin, targets, max_age_ns)
        if not received:
            for target in targets:
                self._targeted[(coin, target)] = None
            return

        chosen_by_target: dict[int, int | None] = {}
        selected_received: set[int] = set()
        for target in targets:
            idx = bisect_right(received, target) - 1
            if idx < 0:
                chosen_by_target[target] = None
                continue
            received_ns = received[idx]
            age_ns = target - received_ns
            if age_ns < 0 or age_ns > max_age_ns:
                chosen_by_target[target] = None
                continue
            chosen_by_target[target] = received_ns
            selected_received.add(received_ns)

        parsed = self._selected_books(coin, selected_received)
        for target in targets:
            received_ns = chosen_by_target.get(target)
            self._targeted[(coin, target)] = (
                parsed.get(received_ns) if received_ns is not None else None
            )

    def prime(self, events: Iterable[Any], scenarios: Iterable[LatencyScenario]) -> None:
        """Resolve all event/scenario book timestamps once before scenario sweeps."""
        scenario_list = tuple(scenarios)
        targets_by_coin: dict[str, list[int]] = defaultdict(list)
        for event in events:
            observed = ObservedSignalLatency(event.exchange_ts_ms, event.received_at_ns)
            coin = wire_coin(event.coin)
            for scenario in scenario_list:
                try:
                    target_ms = observed.estimated_order_arrival_ms(scenario)
                except ValueError:
                    continue
                targets_by_coin[coin].append(self._target_ns(target_ms))

        total = len(targets_by_coin)
        for index, (coin, targets) in enumerate(sorted(targets_by_coin.items()), 1):
            self._resolve_targets(coin, targets)
            if index == 1 or index % 10 == 0 or index == total:
                print(
                    "causal_book_prime "
                    f"coins={index}/{total} resolved_targets={len(self._targeted)} "
                    f"candidate_rows={self.prime_candidate_rows} "
                    f"book_rows={self.prime_book_rows} coin={coin}",
                    flush=True,
                )

    def first_at_or_after(self, coin: str, target_ms: float) -> TapeBook | None:
        target_ns = self._target_ns(target_ms)
        key = (coin, target_ns)
        if key in self._targeted:
            return self._targeted[key]

        if coin in self._cache:
            books = self._load_coin(coin)
            if not books:
                return None
            received_ms = self._received_ms(coin, books)
            idx = bisect_right(received_ms, target_ms) - 1
            if idx < 0:
                return None
            book = books[idx]
            age_ms = target_ms - received_ms[idx]
            return book if 0 <= age_ms <= self.max_age_ms else None

        self._resolve_targets(coin, (target_ns,))
        return self._targeted.get(key)
