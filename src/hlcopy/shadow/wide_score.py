from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from hlcopy.copyability.slippage import estimate_marketable_fill
from hlcopy.market.symbols import canonical_coin, wire_coin
from hlcopy.models import Fill
from hlcopy.positions.state_machine import POSITION_EPSILON, normalize_position
from hlcopy.shadow.evaluator import ParquetL2BookProvider, TapeBook
from hlcopy.shadow.latency import LatencyScenario, ObservedSignalLatency

D = Decimal
ZERO = D("0")
ONE = D("1")
BPS = D("10000")


@dataclass(frozen=True, slots=True)
class WideSignal:
    wallet_id: str
    wallet_address: str
    coin: str
    direction: str
    action: str
    exchange_ts_ms: int
    public_received_at_ns: int
    source_price: Decimal
    tid: int


@dataclass(frozen=True, slots=True)
class WideEpisode:
    wallet_id: str
    wallet_address: str
    coin: str
    direction: str
    entry: WideSignal
    exit: WideSignal | None = None


@dataclass(frozen=True, slots=True)
class WideScoreConfig:
    notional_usd: Decimal = D("1000")
    taker_fee_bps: Decimal = D("4.5")
    max_slippage_bps: Decimal = D("20")
    max_book_forward_ms: int = 750
    horizons_minutes: tuple[int, ...] = (1, 5, 15, 60)


@dataclass(frozen=True, slots=True)
class WideEpisodeScore:
    wallet_id: str
    wallet_address: str
    coin: str
    direction: str
    source_entry_ts_ms: int
    feed_ms: float
    target_entry_ms: float
    entry_vwap: Decimal | None
    entry_slippage_bps: Decimal | None
    markouts_net_bps: dict[str, Decimal | None]
    closed_net_bps: Decimal | None
    closed_source_ts_ms: int | None
    status: str
    reason: str | None

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in row.items()
        }


def _json_rows(folder: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not folder.exists():
        return rows
    for path in sorted(folder.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def load_wide_signals(
    enriched_dir: Path,
    *,
    cutoff_ns: int,
) -> tuple[WideSignal, ...]:
    signals: list[WideSignal] = []
    for row in _json_rows(enriched_dir):
        if row.get("kind") != "wide_official_fill":
            continue
        try:
            public_received = int(row["public_received_at_ns"])
        except (KeyError, TypeError, ValueError):
            continue
        if public_received < cutoff_ns:
            continue
        raw = row.get("official_fill")
        if not isinstance(raw, dict):
            continue
        address = str(row.get("wallet_address") or "").lower()
        try:
            fill = Fill.from_raw(address, raw)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        start = normalize_position(fill.start_position)
        after = normalize_position(start + fill.signed_size)
        if abs(start) <= POSITION_EPSILON:
            start = ZERO
        if abs(after) <= POSITION_EPSILON:
            after = ZERO

        actions: list[tuple[str, str]] = []
        if start == ZERO and after != ZERO:
            actions.append(("OPEN", "LONG" if after > ZERO else "SHORT"))
        elif start != ZERO and (after == ZERO or start * after < ZERO):
            actions.append(("CLOSE", "LONG" if start > ZERO else "SHORT"))
            if after != ZERO:
                actions.append(("OPEN", "LONG" if after > ZERO else "SHORT"))
        if not actions:
            continue

        for action, direction in actions:
            signals.append(
                WideSignal(
                    wallet_id=str(row.get("wallet_id") or address),
                    wallet_address=address,
                    coin=canonical_coin(fill.coin),
                    direction=direction,
                    action=action,
                    exchange_ts_ms=fill.timestamp_ms,
                    public_received_at_ns=public_received,
                    source_price=fill.price,
                    tid=fill.tid,
                )
            )
    signals.sort(
        key=lambda item: (
            item.exchange_ts_ms,
            item.wallet_address,
            item.coin,
            0 if item.action == "CLOSE" else 1,
            item.tid,
        )
    )
    return tuple(signals)


def build_wide_episodes(signals: tuple[WideSignal, ...]) -> tuple[WideEpisode, ...]:
    open_events: dict[tuple[str, str], WideSignal] = {}
    completed: list[WideEpisode] = []
    for signal in signals:
        key = (signal.wallet_address, signal.coin)
        if signal.action == "OPEN":
            open_events[key] = signal
            continue
        entry = open_events.get(key)
        if entry is None or entry.direction != signal.direction:
            continue
        completed.append(
            WideEpisode(
                wallet_id=entry.wallet_id,
                wallet_address=entry.wallet_address,
                coin=entry.coin,
                direction=entry.direction,
                entry=entry,
                exit=signal,
            )
        )
        open_events.pop(key, None)
    still_open = [
        WideEpisode(
            wallet_id=entry.wallet_id,
            wallet_address=entry.wallet_address,
            coin=entry.coin,
            direction=entry.direction,
            entry=entry,
            exit=None,
        )
        for entry in open_events.values()
    ]
    return tuple(
        sorted(
            [*completed, *still_open],
            key=lambda item: (
                item.entry.exchange_ts_ms,
                item.wallet_address,
                item.coin,
            ),
        )
    )


def _book_for(
    provider: ParquetL2BookProvider,
    coin: str,
    target_ms: float,
    max_forward_ms: int,
) -> tuple[TapeBook | None, str | None]:
    book = provider.first_at_or_after(wire_coin(coin), target_ms)
    if book is None:
        return None, "BOOK_MISSING"
    if book.exchange_ts_ms - target_ms > max_forward_ms:
        return None, "BOOK_TOO_FAR_FORWARD"
    return book, None


def _market_vwap(
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
    return estimate.vwap if estimate.complete else None


def _net_round_trip_bps(
    *,
    direction: str,
    entry_vwap: Decimal,
    exit_vwap: Decimal,
    taker_fee_bps: Decimal,
) -> Decimal:
    sign = ONE if direction == "LONG" else D("-1")
    gross = sign * (exit_vwap / entry_vwap - ONE)
    fee = taker_fee_bps / BPS * (ONE + exit_vwap / entry_vwap)
    return (gross - fee) * BPS


def score_wide_episode(
    episode: WideEpisode,
    *,
    provider: ParquetL2BookProvider,
    scenario: LatencyScenario,
    config: WideScoreConfig,
    now_ms: int,
) -> WideEpisodeScore:
    observed = ObservedSignalLatency(
        episode.entry.exchange_ts_ms,
        episode.entry.public_received_at_ns,
    )
    try:
        target_entry = observed.estimated_order_arrival_ms(scenario)
    except ValueError as exc:
        return _failed(episode, feed_ms=observed.feed_ms, reason=f"CLOCK:{exc}")

    entry_book, reason = _book_for(
        provider,
        episode.coin,
        target_entry,
        config.max_book_forward_ms,
    )
    if entry_book is None:
        return _failed(
            episode,
            feed_ms=observed.feed_ms,
            target_entry=target_entry,
            reason=f"ENTRY_{reason}",
        )
    quantity = config.notional_usd / entry_book.mid
    entry_side = "BUY" if episode.direction == "LONG" else "SELL"
    entry_vwap = _market_vwap(
        entry_book,
        side=entry_side,
        quantity=quantity,
        max_slippage_bps=config.max_slippage_bps,
    )
    if entry_vwap is None:
        return _failed(
            episode,
            feed_ms=observed.feed_ms,
            target_entry=target_entry,
            reason="ENTRY_DEPTH_OR_SLIPPAGE",
        )

    sign = ONE if episode.direction == "LONG" else D("-1")
    entry_slip = sign * (entry_vwap / episode.entry.source_price - ONE) * BPS
    exit_side = "SELL" if episode.direction == "LONG" else "BUY"
    markouts: dict[str, Decimal | None] = {}
    for minutes in config.horizons_minutes:
        label = f"{minutes}m"
        horizon_ms = episode.entry.exchange_ts_ms + minutes * 60_000
        if now_ms < horizon_ms:
            markouts[label] = None
            continue
        book, _reason = _book_for(
            provider,
            episode.coin,
            horizon_ms,
            config.max_book_forward_ms,
        )
        if book is None:
            markouts[label] = None
            continue
        exit_vwap = _market_vwap(
            book,
            side=exit_side,
            quantity=quantity,
            max_slippage_bps=config.max_slippage_bps,
        )
        markouts[label] = (
            _net_round_trip_bps(
                direction=episode.direction,
                entry_vwap=entry_vwap,
                exit_vwap=exit_vwap,
                taker_fee_bps=config.taker_fee_bps,
            )
            if exit_vwap is not None
            else None
        )

    closed_net: Decimal | None = None
    close_ts: int | None = None
    status = "OPEN_MARKOUT"
    if episode.exit is not None:
        close_ts = episode.exit.exchange_ts_ms
        close_observed = ObservedSignalLatency(
            episode.exit.exchange_ts_ms,
            episode.exit.public_received_at_ns,
        )
        try:
            close_target = close_observed.estimated_order_arrival_ms(scenario)
        except ValueError:
            close_target = -1
        if close_target >= 0:
            close_book, _close_reason = _book_for(
                provider,
                episode.coin,
                close_target,
                config.max_book_forward_ms,
            )
            if close_book is not None:
                close_vwap = _market_vwap(
                    close_book,
                    side=exit_side,
                    quantity=quantity,
                    max_slippage_bps=config.max_slippage_bps,
                )
                if close_vwap is not None:
                    closed_net = _net_round_trip_bps(
                        direction=episode.direction,
                        entry_vwap=entry_vwap,
                        exit_vwap=close_vwap,
                        taker_fee_bps=config.taker_fee_bps,
                    )
                    status = "CLOSED"
                else:
                    status = "CLOSE_UNEXECUTABLE"
            else:
                status = "CLOSE_BOOK_MISSING"
        else:
            status = "CLOSE_CLOCK_INVALID"

    return WideEpisodeScore(
        wallet_id=episode.wallet_id,
        wallet_address=episode.wallet_address,
        coin=episode.coin,
        direction=episode.direction,
        source_entry_ts_ms=episode.entry.exchange_ts_ms,
        feed_ms=observed.feed_ms,
        target_entry_ms=target_entry,
        entry_vwap=entry_vwap,
        entry_slippage_bps=entry_slip,
        markouts_net_bps=markouts,
        closed_net_bps=closed_net,
        closed_source_ts_ms=close_ts,
        status=status,
        reason=None,
    )


def _failed(
    episode: WideEpisode,
    *,
    feed_ms: float,
    reason: str,
    target_entry: float = 0.0,
) -> WideEpisodeScore:
    return WideEpisodeScore(
        wallet_id=episode.wallet_id,
        wallet_address=episode.wallet_address,
        coin=episode.coin,
        direction=episode.direction,
        source_entry_ts_ms=episode.entry.exchange_ts_ms,
        feed_ms=feed_ms,
        target_entry_ms=target_entry,
        entry_vwap=None,
        entry_slippage_bps=None,
        markouts_net_bps={},
        closed_net_bps=None,
        closed_source_ts_ms=(episode.exit.exchange_ts_ms if episode.exit else None),
        status="UNEXECUTABLE",
        reason=reason,
    )


def wallet_summary(scores: list[WideEpisodeScore]) -> list[dict[str, object]]:
    grouped: dict[str, list[WideEpisodeScore]] = {}
    for score in scores:
        grouped.setdefault(score.wallet_address, []).append(score)
    rows: list[dict[str, object]] = []
    for wallet, wallet_scores in grouped.items():
        executable = [score for score in wallet_scores if score.entry_vwap is not None]
        five_min = [
            value
            for score in executable
            if (value := score.markouts_net_bps.get("5m")) is not None
        ]
        closed = [
            score.closed_net_bps
            for score in executable
            if score.closed_net_bps is not None
        ]
        slips = [
            score.entry_slippage_bps
            for score in executable
            if score.entry_slippage_bps is not None
        ]
        feed = sorted(score.feed_ms for score in wallet_scores)
        rows.append(
            {
                "wallet": wallet,
                "signals": len(wallet_scores),
                "executable": len(executable),
                "execution_pct": (
                    100.0 * len(executable) / len(wallet_scores) if wallet_scores else 0.0
                ),
                "avg_5m_net_bps": (
                    sum(five_min, ZERO) / D(len(five_min)) if five_min else None
                ),
                "median_5m_net_bps": median(five_min) if five_min else None,
                "closed": len(closed),
                "avg_closed_net_bps": (
                    sum(closed, ZERO) / D(len(closed)) if closed else None
                ),
                "closed_win_pct": (
                    D(sum(value > ZERO for value in closed)) / D(len(closed)) * D("100")
                    if closed
                    else None
                ),
                "avg_entry_slip_bps": (
                    sum(slips, ZERO) / D(len(slips)) if slips else None
                ),
                "p95_feed_ms": (
                    feed[min(len(feed) - 1, int((len(feed) - 1) * 0.95))]
                    if feed
                    else None
                ),
            }
        )
    rows.sort(
        key=lambda row: (
            row["closed"],
            row["executable"],
            row["signals"],
        ),
        reverse=True,
    )
    return rows
