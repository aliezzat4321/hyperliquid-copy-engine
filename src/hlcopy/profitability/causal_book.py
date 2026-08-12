from __future__ import annotations

import json
from bisect import bisect_right
from collections import OrderedDict, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

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
    simulated order-arrival timestamps. ``prime`` resolves exactly those timestamps
    with a backward as-of join and JSON-decodes only the selected rows. This avoids
    materializing an entire coin's historical L2 tape for every market touched.

    The inherited full-coin loader remains as a compatibility fallback for tests and
    ad-hoc callers that do not prime the provider first.
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

    @staticmethod
    def _target_ns(target_ms: float) -> int:
        return int(round(float(target_ms) * 1_000_000))

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

    def _resolve_targets(self, coin: str, target_ns_values: Iterable[int]) -> None:
        targets = sorted(
            target for target in set(target_ns_values)
            if (coin, target) not in self._targeted
        )
        if not targets:
            return

        files = sorted(self.market_dir.glob(f"date=*/coin={coin}/channel=l2Book/*.parquet"))
        if not files:
            for target in targets:
                self._targeted[(coin, target)] = None
            return

        max_age_ns = int(self.max_age_ms * 1_000_000)
        lower = targets[0] - max_age_ns
        upper = targets[-1]
        columns = ["exchange_ts_ms", "received_at_ns", "bid_levels_json", "ask_levels_json"]

        scans = [
            pl.scan_parquet(path).select(columns).filter(
                (pl.col("received_at_ns") >= lower)
                & (pl.col("received_at_ns") <= upper)
            )
            for path in files
        ]
        books_frame = pl.concat(scans, how="diagonal_relaxed").sort("received_at_ns").collect()
        if books_frame.is_empty():
            for target in targets:
                self._targeted[(coin, target)] = None
            return

        target_frame = pl.DataFrame({"target_ns": targets}).sort("target_ns")
        joined = target_frame.join_asof(
            books_frame,
            left_on="target_ns",
            right_on="received_at_ns",
            strategy="backward",
        )

        parsed_by_received: dict[int, TapeBook | None] = {}
        for row in joined.iter_rows(named=True):
            target_ns = int(row["target_ns"])
            received = row.get("received_at_ns")
            exchange = row.get("exchange_ts_ms")
            if received is None or exchange is None:
                self._targeted[(coin, target_ns)] = None
                continue
            received_ns = int(received)
            if target_ns - received_ns > max_age_ns:
                self._targeted[(coin, target_ns)] = None
                continue

            book = parsed_by_received.get(received_ns)
            if received_ns not in parsed_by_received:
                bids = self._levels(row.get("bid_levels_json"))
                asks = self._levels(row.get("ask_levels_json"))
                book = (
                    TapeBook(
                        coin=coin,
                        exchange_ts_ms=int(exchange),
                        received_at_ns=received_ns,
                        bids=bids,
                        asks=asks,
                    )
                    if bids and asks
                    else None
                )
                parsed_by_received[received_ns] = book
            self._targeted[(coin, target_ns)] = book

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
                    f"causal_book_prime coins={index}/{total} resolved_targets={len(self._targeted)} coin={coin}",
                    flush=True,
                )

    def first_at_or_after(self, coin: str, target_ms: float) -> TapeBook | None:
        # Keep the historical method name because simulate_copy calls this interface.
        target_ns = self._target_ns(target_ms)
        key = (coin, target_ns)
        if key in self._targeted:
            return self._targeted[key]

        # Compatibility path for unit tests and non-primed callers that explicitly
        # populate/use the inherited full-coin cache.
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

        # Safe targeted fallback: resolve only this timestamp, never the full history.
        self._resolve_targets(coin, (target_ns,))
        return self._targeted.get(key)
