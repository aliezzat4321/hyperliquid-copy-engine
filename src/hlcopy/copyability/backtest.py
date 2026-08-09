from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from hlcopy.copyability.slippage import estimate_marketable_fill
from hlcopy.market.historical_archive import L2Snapshot
from hlcopy.signals.invo import CopySignal

D = Decimal
ZERO = D("0")
BPS = D("10000")


class BookProvider(Protocol):
    def snapshot_at_or_before(self, coin: str, timestamp_ms: int) -> L2Snapshot | None: ...


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    starting_capital: Decimal = D("10000")
    latency_ms: int = 1_000
    follower_leverage: Decimal = D("5")
    taker_fee_rate: Decimal = D("0.00045")
    max_slippage_bps: Decimal = D("20")
    max_margin_fraction_per_trade: Decimal = D("0.05")
    max_total_margin_fraction: Decimal = D("0.50")

    def __post_init__(self) -> None:
        if self.starting_capital <= ZERO:
            raise ValueError("starting_capital must be positive")
        if self.latency_ms < 0:
            raise ValueError("latency_ms cannot be negative")
        if self.follower_leverage <= ZERO:
            raise ValueError("follower_leverage must be positive")
        if not ZERO <= self.taker_fee_rate < D("0.1"):
            raise ValueError("taker_fee_rate is invalid")
        if not ZERO < self.max_margin_fraction_per_trade <= D("1"):
            raise ValueError("max_margin_fraction_per_trade must be within (0,1]")
        if not ZERO < self.max_total_margin_fraction <= D("1"):
            raise ValueError("max_total_margin_fraction must be within (0,1]")


@dataclass(frozen=True, slots=True)
class TradeReplay:
    signal_id: str
    coin: str
    direction: str
    source_opened_at_ms: int
    source_closed_at_ms: int
    status: str
    entry_timestamp_ms: int | None
    exit_timestamp_ms: int | None
    entry_book_age_ms: int | None
    exit_book_age_ms: int | None
    requested_notional: Decimal
    margin_reserved: Decimal
    filled_quantity: Decimal
    entry_vwap: Decimal | None
    exit_vwap: Decimal | None
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    equity_after: Decimal
    source_underlying_return_bps: Decimal
    source_leveraged_return: Decimal
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        for key, value in list(row.items()):
            if isinstance(value, Decimal):
                row[key] = str(value)
        return row


@dataclass(frozen=True, slots=True)
class BacktestSummary:
    mode: str
    latency_ms: int
    follower_leverage: Decimal
    starting_capital: Decimal
    ending_capital: Decimal
    net_pnl: Decimal
    roi: Decimal
    attempted: int
    copied: int
    missed: int
    unresolved: int
    win_rate: Decimal
    max_drawdown: Decimal
    max_margin_reserved: Decimal
    execution_replay_complete: bool
    funding_mode: str
    liquidation_path_mode: str
    equity_sizing_mode: str

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        for key, value in list(row.items()):
            if isinstance(value, Decimal):
                row[key] = str(value)
        return row


@dataclass(slots=True)
class _PreparedSignal:
    signal: CopySignal
    entry_target_ms: int
    exit_target_ms: int
    entry_snapshot: L2Snapshot | None
    exit_snapshot: L2Snapshot | None
    data_gap_reason: str | None = None


@dataclass(slots=True)
class _OpenPosition:
    prepared: _PreparedSignal
    margin_reserved: Decimal
    requested_notional: Decimal
    quantity: Decimal
    entry_price: Decimal
    entry_fee: Decimal
    entry_timestamp_ms: int
    entry_book_age_ms: int | None


def _entry_side(signal: CopySignal) -> str:
    return "BUY" if signal.direction == "LONG" else "SELL"


def _exit_side(signal: CopySignal) -> str:
    return "SELL" if signal.direction == "LONG" else "BUY"


def _market_fill(
    *,
    snapshot: L2Snapshot,
    side: str,
    quantity: Decimal,
    max_slippage_bps: Decimal,
) -> tuple[Decimal, Decimal] | None:
    levels = list(snapshot.asks if side == "BUY" else snapshot.bids)
    estimate = estimate_marketable_fill(
        side=side,
        quantity=quantity,
        levels=levels,
        reference_mid=snapshot.mid,
        max_slippage_bps=max_slippage_bps,
    )
    if not estimate.complete or estimate.vwap is None:
        return None
    return estimate.filled_size, estimate.vwap


def _prepare(
    signals: list[CopySignal] | tuple[CopySignal, ...],
    *,
    latency_ms: int,
    book_provider: BookProvider | None,
) -> list[_PreparedSignal]:
    prepared: list[_PreparedSignal] = []
    for signal in signals:
        entry_target = signal.opened_at_ms + latency_ms
        exit_target = signal.closed_at_ms + latency_ms
        entry_snapshot = None
        exit_snapshot = None
        reason = None
        if book_provider is not None:
            entry_snapshot = book_provider.snapshot_at_or_before(signal.coin, entry_target)
            exit_snapshot = book_provider.snapshot_at_or_before(signal.coin, exit_target)
            if entry_snapshot is None:
                reason = "ENTRY_BOOK_MISSING"
            elif exit_snapshot is None:
                reason = "EXIT_BOOK_MISSING"
        prepared.append(
            _PreparedSignal(
                signal=signal,
                entry_target_ms=entry_target,
                exit_target_ms=exit_target,
                entry_snapshot=entry_snapshot,
                exit_snapshot=exit_snapshot,
                data_gap_reason=reason,
            )
        )
    return prepared


def _missed_replay(
    prepared: _PreparedSignal,
    *,
    equity: Decimal,
    reason: str,
    requested_notional: Decimal = ZERO,
    margin_reserved: Decimal = ZERO,
) -> TradeReplay:
    signal = prepared.signal
    entry_snapshot = prepared.entry_snapshot
    exit_snapshot = prepared.exit_snapshot
    return TradeReplay(
        signal_id=signal.signal_id,
        coin=signal.coin,
        direction=signal.direction,
        source_opened_at_ms=signal.opened_at_ms,
        source_closed_at_ms=signal.closed_at_ms,
        status="MISSED",
        entry_timestamp_ms=entry_snapshot.timestamp_ms if entry_snapshot else None,
        exit_timestamp_ms=exit_snapshot.timestamp_ms if exit_snapshot else None,
        entry_book_age_ms=(
            prepared.entry_target_ms - entry_snapshot.timestamp_ms
            if entry_snapshot is not None
            else None
        ),
        exit_book_age_ms=(
            prepared.exit_target_ms - exit_snapshot.timestamp_ms
            if exit_snapshot is not None
            else None
        ),
        requested_notional=requested_notional,
        margin_reserved=margin_reserved,
        filled_quantity=ZERO,
        entry_vwap=None,
        exit_vwap=None,
        gross_pnl=ZERO,
        fees=ZERO,
        net_pnl=ZERO,
        equity_after=equity,
        source_underlying_return_bps=signal.underlying_return * BPS,
        source_leveraged_return=signal.source_leveraged_return,
        reason=reason,
    )


def run_backtest(
    signals: list[CopySignal] | tuple[CopySignal, ...],
    config: BacktestConfig,
    *,
    book_provider: BookProvider | None = None,
) -> tuple[BacktestSummary, list[TradeReplay]]:
    """Replay copied entries and exits with explicit execution limitations.

    SOURCE_PRICE_BASELINE uses source prices and fees only. L2_EXECUTION uses the
    latest non-future historical book at signal-time + latency and requires enough
    depth inside the slippage cap.

    This version reserves concurrent margin and never uses profit from a trade before
    its close time. It intentionally does not claim mark-to-market liquidation or
    funding accuracy; both limitations are exposed in the summary.
    """

    mode = "L2_EXECUTION" if book_provider is not None else "SOURCE_PRICE_BASELINE"
    prepared = _prepare(
        signals,
        latency_ms=config.latency_ms,
        book_provider=book_provider,
    )
    by_id = {item.signal.signal_id: item for item in prepared}

    events: list[tuple[int, int, str]] = []
    for item in prepared:
        events.append((item.exit_target_ms, 0, item.signal.signal_id))
        events.append((item.entry_target_ms, 1, item.signal.signal_id))
    events.sort()

    equity = config.starting_capital
    peak = equity
    max_drawdown = ZERO
    margin_reserved_total = ZERO
    max_margin_reserved = ZERO
    open_positions: dict[str, _OpenPosition] = {}
    final_rows: dict[str, TradeReplay] = {}

    for _timestamp, event_kind, signal_id in events:
        item = by_id[signal_id]
        signal = item.signal

        if event_kind == 1:
            if item.data_gap_reason is not None:
                final_rows[signal_id] = _missed_replay(
                    item,
                    equity=equity,
                    reason=item.data_gap_reason,
                )
                continue

            desired_fraction = min(
                signal.allocation_fraction,
                config.max_margin_fraction_per_trade,
            )
            total_margin_cap = max(ZERO, equity * config.max_total_margin_fraction)
            available_margin = max(ZERO, total_margin_cap - margin_reserved_total)
            desired_margin = equity * desired_fraction
            margin = min(desired_margin, available_margin)
            if margin <= ZERO:
                final_rows[signal_id] = _missed_replay(
                    item,
                    equity=equity,
                    reason="MARGIN_CAP",
                )
                continue

            requested_notional = margin * config.follower_leverage
            if book_provider is None:
                entry_price = signal.entry_price
                quantity = requested_notional / entry_price
                entry_ts = signal.opened_at_ms
                entry_age = None
            else:
                assert item.entry_snapshot is not None
                quantity_requested = requested_notional / item.entry_snapshot.mid
                fill = _market_fill(
                    snapshot=item.entry_snapshot,
                    side=_entry_side(signal),
                    quantity=quantity_requested,
                    max_slippage_bps=config.max_slippage_bps,
                )
                if fill is None:
                    final_rows[signal_id] = _missed_replay(
                        item,
                        equity=equity,
                        reason="ENTRY_DEPTH_OR_SLIPPAGE",
                        requested_notional=requested_notional,
                        margin_reserved=margin,
                    )
                    continue
                quantity, entry_price = fill
                entry_ts = item.entry_snapshot.timestamp_ms
                entry_age = item.entry_target_ms - entry_ts

            entry_notional = quantity * entry_price
            entry_fee = entry_notional * config.taker_fee_rate
            equity -= entry_fee
            margin_reserved_total += margin
            max_margin_reserved = max(max_margin_reserved, margin_reserved_total)
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity - peak)
            open_positions[signal_id] = _OpenPosition(
                prepared=item,
                margin_reserved=margin,
                requested_notional=requested_notional,
                quantity=quantity,
                entry_price=entry_price,
                entry_fee=entry_fee,
                entry_timestamp_ms=entry_ts,
                entry_book_age_ms=entry_age,
            )
            continue

        position = open_positions.pop(signal_id, None)
        if position is None:
            continue

        if book_provider is None:
            exit_price = signal.exit_price
            exit_ts = signal.closed_at_ms
            exit_age = None
        else:
            assert item.exit_snapshot is not None
            fill = _market_fill(
                snapshot=item.exit_snapshot,
                side=_exit_side(signal),
                quantity=position.quantity,
                max_slippage_bps=config.max_slippage_bps,
            )
            if fill is None:
                final_rows[signal_id] = TradeReplay(
                    signal_id=signal.signal_id,
                    coin=signal.coin,
                    direction=signal.direction,
                    source_opened_at_ms=signal.opened_at_ms,
                    source_closed_at_ms=signal.closed_at_ms,
                    status="UNRESOLVED",
                    entry_timestamp_ms=position.entry_timestamp_ms,
                    exit_timestamp_ms=item.exit_snapshot.timestamp_ms,
                    entry_book_age_ms=position.entry_book_age_ms,
                    exit_book_age_ms=item.exit_target_ms - item.exit_snapshot.timestamp_ms,
                    requested_notional=position.requested_notional,
                    margin_reserved=position.margin_reserved,
                    filled_quantity=position.quantity,
                    entry_vwap=position.entry_price,
                    exit_vwap=None,
                    gross_pnl=ZERO,
                    fees=position.entry_fee,
                    net_pnl=-position.entry_fee,
                    equity_after=equity,
                    source_underlying_return_bps=signal.underlying_return * BPS,
                    source_leveraged_return=signal.source_leveraged_return,
                    reason="EXIT_DEPTH_OR_SLIPPAGE",
                )
                continue
            _filled, exit_price = fill
            exit_ts = item.exit_snapshot.timestamp_ms
            exit_age = item.exit_target_ms - exit_ts

        sign = D("1") if signal.direction == "LONG" else D("-1")
        gross_pnl = sign * position.quantity * (exit_price - position.entry_price)
        exit_notional = position.quantity * exit_price
        exit_fee = exit_notional * config.taker_fee_rate
        fees = position.entry_fee + exit_fee
        net_pnl = gross_pnl - fees

        equity += gross_pnl - exit_fee
        margin_reserved_total -= position.margin_reserved
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)

        final_rows[signal_id] = TradeReplay(
            signal_id=signal.signal_id,
            coin=signal.coin,
            direction=signal.direction,
            source_opened_at_ms=signal.opened_at_ms,
            source_closed_at_ms=signal.closed_at_ms,
            status="COPIED",
            entry_timestamp_ms=position.entry_timestamp_ms,
            exit_timestamp_ms=exit_ts,
            entry_book_age_ms=position.entry_book_age_ms,
            exit_book_age_ms=exit_age,
            requested_notional=position.requested_notional,
            margin_reserved=position.margin_reserved,
            filled_quantity=position.quantity,
            entry_vwap=position.entry_price,
            exit_vwap=exit_price,
            gross_pnl=gross_pnl,
            fees=fees,
            net_pnl=net_pnl,
            equity_after=equity,
            source_underlying_return_bps=signal.underlying_return * BPS,
            source_leveraged_return=signal.source_leveraged_return,
        )

    for signal_id, position in open_positions.items():
        if signal_id in final_rows:
            continue
        item = position.prepared
        signal = item.signal
        final_rows[signal_id] = TradeReplay(
            signal_id=signal.signal_id,
            coin=signal.coin,
            direction=signal.direction,
            source_opened_at_ms=signal.opened_at_ms,
            source_closed_at_ms=signal.closed_at_ms,
            status="UNRESOLVED",
            entry_timestamp_ms=position.entry_timestamp_ms,
            exit_timestamp_ms=None,
            entry_book_age_ms=position.entry_book_age_ms,
            exit_book_age_ms=None,
            requested_notional=position.requested_notional,
            margin_reserved=position.margin_reserved,
            filled_quantity=position.quantity,
            entry_vwap=position.entry_price,
            exit_vwap=None,
            gross_pnl=ZERO,
            fees=position.entry_fee,
            net_pnl=-position.entry_fee,
            equity_after=equity,
            source_underlying_return_bps=signal.underlying_return * BPS,
            source_leveraged_return=signal.source_leveraged_return,
            reason="POSITION_LEFT_OPEN",
        )

    rows = [
        final_rows[item.signal.signal_id]
        for item in prepared
        if item.signal.signal_id in final_rows
    ]
    copied = [row for row in rows if row.status == "COPIED"]
    unresolved = [row for row in rows if row.status == "UNRESOLVED"]
    wins = sum(row.net_pnl > ZERO for row in copied)
    net_pnl = equity - config.starting_capital
    summary = BacktestSummary(
        mode=mode,
        latency_ms=config.latency_ms,
        follower_leverage=config.follower_leverage,
        starting_capital=config.starting_capital,
        ending_capital=equity,
        net_pnl=net_pnl,
        roi=net_pnl / config.starting_capital,
        attempted=len(rows),
        copied=len(copied),
        missed=sum(row.status == "MISSED" for row in rows),
        unresolved=len(unresolved),
        win_rate=D(wins) / D(len(copied)) if copied else ZERO,
        max_drawdown=max_drawdown,
        max_margin_reserved=max_margin_reserved,
        execution_replay_complete=not unresolved,
        funding_mode="NOT_MODELED",
        liquidation_path_mode="NOT_MODELED",
        equity_sizing_mode="REALIZED_EQUITY_WITH_CONCURRENT_MARGIN_RESERVATION",
    )
    return summary, rows


def write_backtest_outputs(
    output_dir: Path,
    *,
    stem: str,
    summary: BacktestSummary,
    rows: list[TradeReplay],
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{stem}.json"
    csv_path = output_dir / f"{stem}.csv"
    json_path.write_text(
        json.dumps(
            {"summary": summary.to_dict(), "trades": [row.to_dict() for row in rows]},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    fieldnames = list(rows[0].to_dict()) if rows else list(TradeReplay.__dataclass_fields__)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_dict())
    return json_path, csv_path
