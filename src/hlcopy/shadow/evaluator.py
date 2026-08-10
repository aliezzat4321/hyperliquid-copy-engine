from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Protocol

import polars as pl

from hlcopy.copyability.slippage import BookLevel, estimate_marketable_fill
from hlcopy.models import Fill
from hlcopy.positions.state_machine import POSITION_EPSILON, normalize_position
from hlcopy.shadow.latency import LatencyScenario, ObservedSignalLatency

D = Decimal
ZERO = D("0")
ONE = D("1")
HUNDRED = D("100")
BPS = D("10000")


@dataclass(frozen=True, slots=True)
class SourceEvent:
    wallet_id: str
    wallet_address: str
    coin: str
    action: str
    direction: str
    exchange_ts_ms: int
    received_at_ns: int
    source_fill_price: Decimal
    source_tid: int


@dataclass(frozen=True, slots=True)
class SourceEpisode:
    wallet_id: str
    wallet_address: str
    coin: str
    direction: str
    entry: SourceEvent
    exit: SourceEvent


@dataclass(frozen=True, slots=True)
class TapeBook:
    coin: str
    exchange_ts_ms: int
    received_at_ns: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    @property
    def mid(self) -> Decimal:
        if not self.bids or not self.asks:
            raise ValueError("book requires both bid and ask")
        return (self.bids[0].price + self.asks[0].price) / D("2")


class BookProvider(Protocol):
    def first_at_or_after(self, coin: str, exchange_ts_ms: float) -> TapeBook | None: ...


class ParquetL2BookProvider:
    """Reads append-only local L2 tape and returns the first published book at/after a target."""

    def __init__(self, market_dir: Path) -> None:
        self.market_dir = market_dir
        self._cache: dict[str, list[TapeBook]] = {}

    def _load_coin(self, coin: str) -> list[TapeBook]:
        if coin in self._cache:
            return self._cache[coin]
        files = sorted(
            self.market_dir.glob(f"date=*/coin={coin}/channel=l2Book/*.parquet")
        )
        if not files:
            self._cache[coin] = []
            return []
        frame = pl.concat(
            [pl.read_parquet(path) for path in files],
            how="diagonal_relaxed",
        ).sort(["exchange_ts_ms", "received_at_ns"])
        books: list[TapeBook] = []
        for row in frame.iter_rows(named=True):
            if row.get("exchange_ts_ms") is None or row.get("received_at_ns") is None:
                continue
            try:
                raw_bids = json.loads(str(row.get("bid_levels_json") or "[]"))
                raw_asks = json.loads(str(row.get("ask_levels_json") or "[]"))
                bids = tuple(
                    BookLevel(D(str(level["px"])), D(str(level["sz"])))
                    for level in raw_bids
                    if D(str(level.get("px", "0"))) > ZERO
                    and D(str(level.get("sz", "0"))) > ZERO
                )
                asks = tuple(
                    BookLevel(D(str(level["px"])), D(str(level["sz"])))
                    for level in raw_asks
                    if D(str(level.get("px", "0"))) > ZERO
                    and D(str(level.get("sz", "0"))) > ZERO
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, ArithmeticError):
                continue
            if not bids or not asks:
                continue
            books.append(
                TapeBook(
                    coin=coin,
                    exchange_ts_ms=int(row["exchange_ts_ms"]),
                    received_at_ns=int(row["received_at_ns"]),
                    bids=bids,
                    asks=asks,
                )
            )
        self._cache[coin] = books
        return books

    def first_at_or_after(self, coin: str, exchange_ts_ms: float) -> TapeBook | None:
        books = self._load_coin(coin)
        lo = 0
        hi = len(books)
        while lo < hi:
            mid = (lo + hi) // 2
            if books[mid].exchange_ts_ms < exchange_ts_ms:
                lo = mid + 1
            else:
                hi = mid
        return books[lo] if lo < len(books) else None


def load_prospective_episodes(shadow_dir: Path, wallet_id: str) -> tuple[SourceEpisode, ...]:
    records: list[tuple[int, int, dict[str, object]]] = []
    for path in sorted((shadow_dir / "fills").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("kind") != "wallet_fill" or row.get("wallet_id") != wallet_id:
                continue
            if row.get("is_snapshot"):
                continue
            fill = row.get("fill")
            if not isinstance(fill, dict):
                continue
            try:
                exchange_ts_ms = int(fill["time"])
                tid = int(fill["tid"])
            except (KeyError, TypeError, ValueError):
                continue
            records.append((exchange_ts_ms, tid, row))
    records.sort(key=lambda value: (value[0], value[1]))

    open_events: dict[str, SourceEvent] = {}
    episodes: list[SourceEpisode] = []
    for _ts, _tid, row in records:
        fill_raw = row["fill"]
        assert isinstance(fill_raw, dict)
        address = str(row.get("wallet_address") or "")
        fill = Fill.from_raw(address, fill_raw)
        start = normalize_position(fill.start_position)
        after = normalize_position(start + fill.signed_size)
        if abs(start) <= POSITION_EPSILON:
            start = ZERO
        if abs(after) <= POSITION_EPSILON:
            after = ZERO
        received_at_ns = int(row["received_at_ns"])
        event_common = {
            "wallet_id": wallet_id,
            "wallet_address": address,
            "coin": fill.coin,
            "exchange_ts_ms": fill.timestamp_ms,
            "received_at_ns": received_at_ns,
            "source_fill_price": fill.price,
            "source_tid": fill.tid,
        }

        if start == ZERO and after != ZERO:
            direction = "LONG" if after > ZERO else "SHORT"
            open_events[fill.coin] = SourceEvent(
                action="OPEN",
                direction=direction,
                **event_common,
            )
            continue

        if start != ZERO and (after == ZERO or start * after < ZERO):
            direction = "LONG" if start > ZERO else "SHORT"
            entry = open_events.pop(fill.coin, None)
            if entry is not None and entry.direction == direction:
                exit_event = SourceEvent(
                    action="CLOSE",
                    direction=direction,
                    **event_common,
                )
                episodes.append(
                    SourceEpisode(
                        wallet_id=wallet_id,
                        wallet_address=address,
                        coin=fill.coin,
                        direction=direction,
                        entry=entry,
                        exit=exit_event,
                    )
                )
            if after != ZERO:
                flipped = "LONG" if after > ZERO else "SHORT"
                open_events[fill.coin] = SourceEvent(
                    action="OPEN",
                    direction=flipped,
                    **event_common,
                )
    return tuple(episodes)


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    notional_usd: Decimal
    follower_leverage: Decimal
    taker_fee_bps: Decimal
    max_slippage_bps: Decimal
    max_book_forward_ms: int = 750

    def __post_init__(self) -> None:
        if self.notional_usd <= ZERO:
            raise ValueError("notional_usd must be positive")
        if self.follower_leverage <= ZERO:
            raise ValueError("follower_leverage must be positive")
        if self.taker_fee_bps < ZERO:
            raise ValueError("taker_fee_bps cannot be negative")
        if self.max_slippage_bps <= ZERO:
            raise ValueError("max_slippage_bps must be positive")
        if self.max_book_forward_ms < 0:
            raise ValueError("max_book_forward_ms cannot be negative")


@dataclass(frozen=True, slots=True)
class EpisodeExecution:
    wallet_id: str
    coin: str
    direction: str
    status: str
    source_entry_ts_ms: int
    source_exit_ts_ms: int
    source_entry_price: Decimal
    source_exit_price: Decimal
    entry_signal_feed_ms: float
    exit_signal_feed_ms: float
    entry_target_arrival_ms: float
    exit_target_arrival_ms: float
    entry_book_ts_ms: int | None
    exit_book_ts_ms: int | None
    entry_book_forward_ms: float | None
    exit_book_forward_ms: float | None
    entry_vwap: Decimal | None
    exit_vwap: Decimal | None
    gross_underlying_bps: Decimal | None
    net_underlying_bps: Decimal | None
    net_return_on_margin_pct: Decimal | None
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in row.items()
        }


def _event_target(event: SourceEvent, scenario: LatencyScenario) -> tuple[float, float]:
    observed = ObservedSignalLatency(event.exchange_ts_ms, event.received_at_ns)
    return observed.feed_ms, observed.estimated_order_arrival_ms(scenario)


def _market_fill(
    book: TapeBook,
    *,
    side: str,
    quantity: Decimal,
    max_slippage_bps: Decimal,
) -> Decimal | None:
    levels = list(book.asks if side == "BUY" else book.bids)
    estimate = estimate_marketable_fill(
        side=side,
        quantity=quantity,
        levels=levels,
        reference_mid=book.mid,
        max_slippage_bps=max_slippage_bps,
    )
    if not estimate.complete:
        return None
    return estimate.vwap


def evaluate_episode(
    episode: SourceEpisode,
    *,
    provider: BookProvider,
    scenario: LatencyScenario,
    config: ExecutionConfig,
) -> EpisodeExecution:
    try:
        entry_feed, entry_target = _event_target(episode.entry, scenario)
        exit_feed, exit_target = _event_target(episode.exit, scenario)
    except ValueError as exc:
        return _failed(episode, reason=f"CLOCK:{exc}")

    entry_book = provider.first_at_or_after(episode.coin, entry_target)
    exit_book = provider.first_at_or_after(episode.coin, exit_target)
    if entry_book is None:
        return _failed(
            episode,
            reason="ENTRY_BOOK_MISSING",
            entry_feed=entry_feed,
            exit_feed=exit_feed,
            entry_target=entry_target,
            exit_target=exit_target,
        )
    if exit_book is None:
        return _failed(
            episode,
            reason="EXIT_BOOK_MISSING",
            entry_feed=entry_feed,
            exit_feed=exit_feed,
            entry_target=entry_target,
            exit_target=exit_target,
            entry_book=entry_book,
        )
    entry_forward = entry_book.exchange_ts_ms - entry_target
    exit_forward = exit_book.exchange_ts_ms - exit_target
    if entry_forward > config.max_book_forward_ms:
        return _failed(
            episode,
            reason="ENTRY_BOOK_TOO_FAR_FORWARD",
            entry_feed=entry_feed,
            exit_feed=exit_feed,
            entry_target=entry_target,
            exit_target=exit_target,
            entry_book=entry_book,
            exit_book=exit_book,
        )
    if exit_forward > config.max_book_forward_ms:
        return _failed(
            episode,
            reason="EXIT_BOOK_TOO_FAR_FORWARD",
            entry_feed=entry_feed,
            exit_feed=exit_feed,
            entry_target=entry_target,
            exit_target=exit_target,
            entry_book=entry_book,
            exit_book=exit_book,
        )

    entry_side = "BUY" if episode.direction == "LONG" else "SELL"
    exit_side = "SELL" if episode.direction == "LONG" else "BUY"
    quantity = config.notional_usd / entry_book.mid
    entry_vwap = _market_fill(
        entry_book,
        side=entry_side,
        quantity=quantity,
        max_slippage_bps=config.max_slippage_bps,
    )
    if entry_vwap is None:
        return _failed(
            episode,
            reason="ENTRY_DEPTH_OR_SLIPPAGE",
            entry_feed=entry_feed,
            exit_feed=exit_feed,
            entry_target=entry_target,
            exit_target=exit_target,
            entry_book=entry_book,
            exit_book=exit_book,
        )
    exit_vwap = _market_fill(
        exit_book,
        side=exit_side,
        quantity=quantity,
        max_slippage_bps=config.max_slippage_bps,
    )
    if exit_vwap is None:
        return _failed(
            episode,
            reason="EXIT_DEPTH_OR_SLIPPAGE",
            entry_feed=entry_feed,
            exit_feed=exit_feed,
            entry_target=entry_target,
            exit_target=exit_target,
            entry_book=entry_book,
            exit_book=exit_book,
            entry_vwap=entry_vwap,
        )

    sign = ONE if episode.direction == "LONG" else D("-1")
    underlying = sign * (exit_vwap / entry_vwap - ONE)
    fee_rate = config.taker_fee_bps / BPS
    fee_underlying = fee_rate * (ONE + exit_vwap / entry_vwap)
    net_underlying = underlying - fee_underlying
    net_margin_pct = net_underlying * config.follower_leverage * HUNDRED
    return EpisodeExecution(
        wallet_id=episode.wallet_id,
        coin=episode.coin,
        direction=episode.direction,
        status="EXECUTED",
        source_entry_ts_ms=episode.entry.exchange_ts_ms,
        source_exit_ts_ms=episode.exit.exchange_ts_ms,
        source_entry_price=episode.entry.source_fill_price,
        source_exit_price=episode.exit.source_fill_price,
        entry_signal_feed_ms=entry_feed,
        exit_signal_feed_ms=exit_feed,
        entry_target_arrival_ms=entry_target,
        exit_target_arrival_ms=exit_target,
        entry_book_ts_ms=entry_book.exchange_ts_ms,
        exit_book_ts_ms=exit_book.exchange_ts_ms,
        entry_book_forward_ms=entry_forward,
        exit_book_forward_ms=exit_forward,
        entry_vwap=entry_vwap,
        exit_vwap=exit_vwap,
        gross_underlying_bps=underlying * BPS,
        net_underlying_bps=net_underlying * BPS,
        net_return_on_margin_pct=net_margin_pct,
    )


def _failed(
    episode: SourceEpisode,
    *,
    reason: str,
    entry_feed: float = 0.0,
    exit_feed: float = 0.0,
    entry_target: float = 0.0,
    exit_target: float = 0.0,
    entry_book: TapeBook | None = None,
    exit_book: TapeBook | None = None,
    entry_vwap: Decimal | None = None,
) -> EpisodeExecution:
    return EpisodeExecution(
        wallet_id=episode.wallet_id,
        coin=episode.coin,
        direction=episode.direction,
        status="MISSED",
        source_entry_ts_ms=episode.entry.exchange_ts_ms,
        source_exit_ts_ms=episode.exit.exchange_ts_ms,
        source_entry_price=episode.entry.source_fill_price,
        source_exit_price=episode.exit.source_fill_price,
        entry_signal_feed_ms=entry_feed,
        exit_signal_feed_ms=exit_feed,
        entry_target_arrival_ms=entry_target,
        exit_target_arrival_ms=exit_target,
        entry_book_ts_ms=entry_book.exchange_ts_ms if entry_book else None,
        exit_book_ts_ms=exit_book.exchange_ts_ms if exit_book else None,
        entry_book_forward_ms=(entry_book.exchange_ts_ms - entry_target if entry_book else None),
        exit_book_forward_ms=(exit_book.exchange_ts_ms - exit_target if exit_book else None),
        entry_vwap=entry_vwap,
        exit_vwap=None,
        gross_underlying_bps=None,
        net_underlying_bps=None,
        net_return_on_margin_pct=None,
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    wallet_id: str
    scenario_name: str
    notional_usd: Decimal
    follower_leverage: Decimal
    completed_source_episodes: int
    executed: int
    missed: int
    execution_fraction: Decimal
    avg_net_underlying_bps: Decimal | None
    median_net_underlying_bps: Decimal | None
    avg_net_return_on_margin_pct: Decimal | None
    median_net_return_on_margin_pct: Decimal | None
    net_win_rate: Decimal | None
    p95_entry_signal_feed_ms: float | None
    p95_exit_signal_feed_ms: float | None
    funding_mode: str = "NOT_MODELED"
    liquidation_path_mode: str = "NOT_MODELED"
    book_alignment_mode: str = "FIRST_L2_AT_OR_AFTER_ESTIMATED_ORDER_ARRIVAL"

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in row.items()
        }


def _percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * probability)))
    return ordered[index]


def summarize_executions(
    wallet_id: str,
    scenario: LatencyScenario,
    config: ExecutionConfig,
    rows: list[EpisodeExecution],
) -> EvaluationSummary:
    executed = [row for row in rows if row.status == "EXECUTED"]
    net_bps = [row.net_underlying_bps for row in executed if row.net_underlying_bps is not None]
    net_margin = [
        row.net_return_on_margin_pct
        for row in executed
        if row.net_return_on_margin_pct is not None
    ]
    wins = sum(value > ZERO for value in net_margin)
    total = len(rows)
    return EvaluationSummary(
        wallet_id=wallet_id,
        scenario_name=scenario.name,
        notional_usd=config.notional_usd,
        follower_leverage=config.follower_leverage,
        completed_source_episodes=total,
        executed=len(executed),
        missed=total - len(executed),
        execution_fraction=D(len(executed)) / D(total) if total else ZERO,
        avg_net_underlying_bps=(sum(net_bps, ZERO) / D(len(net_bps)) if net_bps else None),
        median_net_underlying_bps=(D(str(median(net_bps))) if net_bps else None),
        avg_net_return_on_margin_pct=(
            sum(net_margin, ZERO) / D(len(net_margin)) if net_margin else None
        ),
        median_net_return_on_margin_pct=(D(str(median(net_margin))) if net_margin else None),
        net_win_rate=D(wins) / D(len(net_margin)) if net_margin else None,
        p95_entry_signal_feed_ms=_percentile(
            [row.entry_signal_feed_ms for row in rows],
            0.95,
        ),
        p95_exit_signal_feed_ms=_percentile(
            [row.exit_signal_feed_ms for row in rows],
            0.95,
        ),
    )
