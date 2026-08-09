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
    win_rate: Decimal
    max_drawdown: Decimal

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        for key, value in list(row.items()):
            if isinstance(value, Decimal):
                row[key] = str(value)
        return row


def _source_execution_prices(signal: CopySignal) -> tuple[Decimal, Decimal]:
    return signal.entry_price, signal.exit_price


def _entry_side(signal: CopySignal) -> str:
    return "BUY" if signal.direction == "LONG" else "SELL"


def _exit_side(signal: CopySignal) -> str:
    return "SELL" if signal.direction == "LONG" else "BUY"


def _market_fill(
    *,
    snapshot: L2Snapshot,
    side: str,
    notional: Decimal,
    max_slippage_bps: Decimal,
) -> tuple[Decimal, Decimal] | None:
    quantity = notional / snapshot.mid
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


def run_backtest(
    signals: list[CopySignal] | tuple[CopySignal, ...],
    config: BacktestConfig,
    *,
    book_provider: BookProvider | None = None,
) -> tuple[BacktestSummary, list[TradeReplay]]:
    """Replay copied entries/exits.

    Without a book provider this is a SOURCE_PRICE_BASELINE: it uses the source entry
    and exit prices, applies follower sizing and taker fees, and must never be presented
    as execution proof.

    With a provider, the follower executes against the latest non-future L2 snapshot at
    entry/exit signal time plus configured latency and requires a complete marketable
    fill within the slippage cap. Missing/stale books or insufficient depth are skipped.
    """

    mode = "L2_EXECUTION" if book_provider is not None else "SOURCE_PRICE_BASELINE"
    equity = config.starting_capital
    peak = equity
    max_drawdown = ZERO
    replays: list[TradeReplay] = []

    # Conservative V1 accounting: entries are sized using equity available when their
    # replay is processed. We process by source close time so realized PnL is never used
    # before it existed. Exact concurrent-margin accounting is a later execution layer.
    ordered = sorted(
        signals,
        key=lambda item: (item.closed_at_ms, item.opened_at_ms, item.signal_id),
    )

    for signal in ordered:
        requested_margin_fraction = min(
            signal.allocation_fraction,
            config.max_margin_fraction_per_trade,
        )
        requested_notional = equity * requested_margin_fraction * config.follower_leverage
        if requested_notional <= ZERO:
            replays.append(
                TradeReplay(
                    signal_id=signal.signal_id,
                    coin=signal.coin,
                    direction=signal.direction,
                    source_opened_at_ms=signal.opened_at_ms,
                    source_closed_at_ms=signal.closed_at_ms,
                    status="MISSED",
                    entry_timestamp_ms=None,
                    exit_timestamp_ms=None,
                    entry_book_age_ms=None,
                    exit_book_age_ms=None,
                    requested_notional=ZERO,
                    filled_quantity=ZERO,
                    entry_vwap=None,
                    exit_vwap=None,
                    gross_pnl=ZERO,
                    fees=ZERO,
                    net_pnl=ZERO,
                    equity_after=equity,
                    source_underlying_return_bps=signal.underlying_return * BPS,
                    source_leveraged_return=signal.source_leveraged_return,
                    reason="ZERO_NOTIONAL",
                )
            )
            continue

        entry_target = signal.opened_at_ms + config.latency_ms
        exit_target = signal.closed_at_ms + config.latency_ms
        entry_book_age: int | None = None
        exit_book_age: int | None = None

        if book_provider is None:
            entry_price, exit_price = _source_execution_prices(signal)
            quantity = requested_notional / entry_price
            entry_ts = signal.opened_at_ms
            exit_ts = signal.closed_at_ms
        else:
            entry_snapshot = book_provider.snapshot_at_or_before(signal.coin, entry_target)
            exit_snapshot = book_provider.snapshot_at_or_before(signal.coin, exit_target)
            if entry_snapshot is None or exit_snapshot is None:
                reason = "ENTRY_BOOK_MISSING" if entry_snapshot is None else "EXIT_BOOK_MISSING"
                replays.append(
                    TradeReplay(
                        signal_id=signal.signal_id,
                        coin=signal.coin,
                        direction=signal.direction,
                        source_opened_at_ms=signal.opened_at_ms,
                        source_closed_at_ms=signal.closed_at_ms,
                        status="MISSED",
                        entry_timestamp_ms=entry_snapshot.timestamp_ms if entry_snapshot else None,
                        exit_timestamp_ms=exit_snapshot.timestamp_ms if exit_snapshot else None,
                        entry_book_age_ms=(
                            entry_target - entry_snapshot.timestamp_ms
                            if entry_snapshot
                            else None
                        ),
                        exit_book_age_ms=(
                            exit_target - exit_snapshot.timestamp_ms
                            if exit_snapshot
                            else None
                        ),
                        requested_notional=requested_notional,
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
                )
                continue

            entry_book_age = entry_target - entry_snapshot.timestamp_ms
            exit_book_age = exit_target - exit_snapshot.timestamp_ms
            entry_fill = _market_fill(
                snapshot=entry_snapshot,
                side=_entry_side(signal),
                notional=requested_notional,
                max_slippage_bps=config.max_slippage_bps,
            )
            if entry_fill is None:
                replays.append(
                    TradeReplay(
                        signal_id=signal.signal_id,
                        coin=signal.coin,
                        direction=signal.direction,
                        source_opened_at_ms=signal.opened_at_ms,
                        source_closed_at_ms=signal.closed_at_ms,
                        status="MISSED",
                        entry_timestamp_ms=entry_snapshot.timestamp_ms,
                        exit_timestamp_ms=exit_snapshot.timestamp_ms,
                        entry_book_age_ms=entry_book_age,
                        exit_book_age_ms=exit_book_age,
                        requested_notional=requested_notional,
                        filled_quantity=ZERO,
                        entry_vwap=None,
                        exit_vwap=None,
                        gross_pnl=ZERO,
                        fees=ZERO,
                        net_pnl=ZERO,
                        equity_after=equity,
                        source_underlying_return_bps=signal.underlying_return * BPS,
                        source_leveraged_return=signal.source_leveraged_return,
                        reason="ENTRY_DEPTH_OR_SLIPPAGE",
                    )
                )
                continue
            quantity, entry_price = entry_fill

            exit_levels = list(
                exit_snapshot.bids if _exit_side(signal) == "SELL" else exit_snapshot.asks
            )
            exit_estimate = estimate_marketable_fill(
                side=_exit_side(signal),
                quantity=quantity,
                levels=exit_levels,
                reference_mid=exit_snapshot.mid,
                max_slippage_bps=config.max_slippage_bps,
            )
            if not exit_estimate.complete or exit_estimate.vwap is None:
                replays.append(
                    TradeReplay(
                        signal_id=signal.signal_id,
                        coin=signal.coin,
                        direction=signal.direction,
                        source_opened_at_ms=signal.opened_at_ms,
                        source_closed_at_ms=signal.closed_at_ms,
                        status="MISSED",
                        entry_timestamp_ms=entry_snapshot.timestamp_ms,
                        exit_timestamp_ms=exit_snapshot.timestamp_ms,
                        entry_book_age_ms=entry_book_age,
                        exit_book_age_ms=exit_book_age,
                        requested_notional=requested_notional,
                        filled_quantity=quantity,
                        entry_vwap=entry_price,
                        exit_vwap=None,
                        gross_pnl=ZERO,
                        fees=ZERO,
                        net_pnl=ZERO,
                        equity_after=equity,
                        source_underlying_return_bps=signal.underlying_return * BPS,
                        source_leveraged_return=signal.source_leveraged_return,
                        reason="EXIT_DEPTH_OR_SLIPPAGE",
                    )
                )
                continue
            exit_price = exit_estimate.vwap
            entry_ts = entry_snapshot.timestamp_ms
            exit_ts = exit_snapshot.timestamp_ms

        sign = D("1") if signal.direction == "LONG" else D("-1")
        gross_pnl = sign * quantity * (exit_price - entry_price)
        entry_notional = quantity * entry_price
        exit_notional = quantity * exit_price
        fees = (entry_notional + exit_notional) * config.taker_fee_rate
        net_pnl = gross_pnl - fees
        equity += net_pnl
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)

        replays.append(
            TradeReplay(
                signal_id=signal.signal_id,
                coin=signal.coin,
                direction=signal.direction,
                source_opened_at_ms=signal.opened_at_ms,
                source_closed_at_ms=signal.closed_at_ms,
                status="COPIED",
                entry_timestamp_ms=entry_ts,
                exit_timestamp_ms=exit_ts,
                entry_book_age_ms=entry_book_age,
                exit_book_age_ms=exit_book_age,
                requested_notional=requested_notional,
                filled_quantity=quantity,
                entry_vwap=entry_price,
                exit_vwap=exit_price,
                gross_pnl=gross_pnl,
                fees=fees,
                net_pnl=net_pnl,
                equity_after=equity,
                source_underlying_return_bps=signal.underlying_return * BPS,
                source_leveraged_return=signal.source_leveraged_return,
            )
        )

    copied = [row for row in replays if row.status == "COPIED"]
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
        attempted=len(replays),
        copied=len(copied),
        missed=len(replays) - len(copied),
        win_rate=D(wins) / D(len(copied)) if copied else ZERO,
        max_drawdown=max_drawdown,
    )
    return summary, replays


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
