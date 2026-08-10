from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median

from hlcopy.copyability.slippage import estimate_marketable_fill
from hlcopy.market.historical_archive import L2Snapshot, LocalArchiveBookProvider
from hlcopy.signals.invo import CopySignal, load_invo_closed_trades

D = Decimal
ZERO = D("0")
ONE = D("1")
HUNDRED = D("100")
BPS = D("10000")


@dataclass(frozen=True, slots=True)
class PercentReplayTrade:
    signal_id: str
    coin: str
    direction: str
    source_leverage: Decimal
    source_return_pct: Decimal
    source_liquidated: bool
    latency_ms: int
    margin_usd: Decimal
    status: str
    entry_vwap: Decimal | None
    exit_vwap: Decimal | None
    gross_return_pct: Decimal | None
    fee_drag_pct: Decimal | None
    net_return_pct: Decimal | None
    entry_book_age_ms: int | None
    exit_book_age_ms: int | None
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in row.items()
        }


@dataclass(frozen=True, slots=True)
class PercentReplaySummary:
    mode: str
    latency_ms: int
    margin_usd: Decimal
    attempted: int
    executable: int
    missed: int
    source_liquidations: int
    source_avg_return_pct: Decimal
    source_median_return_pct: Decimal
    source_win_rate: Decimal
    follower_avg_gross_return_pct: Decimal | None
    follower_avg_net_return_pct: Decimal | None
    follower_median_net_return_pct: Decimal | None
    follower_net_win_rate: Decimal | None
    avg_net_edge_retention: Decimal | None
    execution_complete: bool
    liquidation_path_mode: str = "NOT_MODELED"
    funding_mode: str = "NOT_MODELED"

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in row.items()
        }


def _positive_decimal(value: str) -> Decimal:
    try:
        parsed = D(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("value must be numeric") from exc
    if parsed <= ZERO:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value cannot be negative")
    return parsed


def _since_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        if "T" in value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise SystemExit("--since must be YYYY-MM-DD or ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _entry_side(signal: CopySignal) -> str:
    return "BUY" if signal.direction == "LONG" else "SELL"


def _exit_side(signal: CopySignal) -> str:
    return "SELL" if signal.direction == "LONG" else "BUY"


def _fill(
    snapshot: L2Snapshot,
    *,
    side: str,
    quantity: Decimal,
    max_slippage_bps: Decimal,
) -> Decimal | None:
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
    return estimate.vwap


def replay_trade(
    signal: CopySignal,
    *,
    latency_ms: int,
    margin_usd: Decimal,
    taker_fee_rate: Decimal,
    max_slippage_bps: Decimal,
    provider: LocalArchiveBookProvider | None,
) -> PercentReplayTrade:
    source_return_pct = signal.source_leveraged_return * HUNDRED
    requested_notional = margin_usd * signal.source_leverage

    if provider is None:
        entry_price = signal.entry_price
        exit_price = signal.exit_price
        entry_age = None
        exit_age = None
        quantity = requested_notional / entry_price
    else:
        entry_target = signal.opened_at_ms + latency_ms
        exit_target = signal.closed_at_ms + latency_ms
        entry_snapshot = provider.snapshot_at_or_before(signal.coin, entry_target)
        exit_snapshot = provider.snapshot_at_or_before(signal.coin, exit_target)
        if entry_snapshot is None:
            return PercentReplayTrade(
                signal_id=signal.signal_id,
                coin=signal.coin,
                direction=signal.direction,
                source_leverage=signal.source_leverage,
                source_return_pct=source_return_pct,
                source_liquidated=signal.liquidated,
                latency_ms=latency_ms,
                margin_usd=margin_usd,
                status="MISSED",
                entry_vwap=None,
                exit_vwap=None,
                gross_return_pct=None,
                fee_drag_pct=None,
                net_return_pct=None,
                entry_book_age_ms=None,
                exit_book_age_ms=(
                    exit_target - exit_snapshot.timestamp_ms
                    if exit_snapshot is not None
                    else None
                ),
                reason="ENTRY_BOOK_MISSING",
            )
        if exit_snapshot is None:
            return PercentReplayTrade(
                signal_id=signal.signal_id,
                coin=signal.coin,
                direction=signal.direction,
                source_leverage=signal.source_leverage,
                source_return_pct=source_return_pct,
                source_liquidated=signal.liquidated,
                latency_ms=latency_ms,
                margin_usd=margin_usd,
                status="MISSED",
                entry_vwap=None,
                exit_vwap=None,
                gross_return_pct=None,
                fee_drag_pct=None,
                net_return_pct=None,
                entry_book_age_ms=entry_target - entry_snapshot.timestamp_ms,
                exit_book_age_ms=None,
                reason="EXIT_BOOK_MISSING",
            )

        quantity = requested_notional / entry_snapshot.mid
        entry_price = _fill(
            entry_snapshot,
            side=_entry_side(signal),
            quantity=quantity,
            max_slippage_bps=max_slippage_bps,
        )
        if entry_price is None:
            return PercentReplayTrade(
                signal_id=signal.signal_id,
                coin=signal.coin,
                direction=signal.direction,
                source_leverage=signal.source_leverage,
                source_return_pct=source_return_pct,
                source_liquidated=signal.liquidated,
                latency_ms=latency_ms,
                margin_usd=margin_usd,
                status="MISSED",
                entry_vwap=None,
                exit_vwap=None,
                gross_return_pct=None,
                fee_drag_pct=None,
                net_return_pct=None,
                entry_book_age_ms=entry_target - entry_snapshot.timestamp_ms,
                exit_book_age_ms=exit_target - exit_snapshot.timestamp_ms,
                reason="ENTRY_DEPTH_OR_SLIPPAGE",
            )
        exit_price = _fill(
            exit_snapshot,
            side=_exit_side(signal),
            quantity=quantity,
            max_slippage_bps=max_slippage_bps,
        )
        if exit_price is None:
            return PercentReplayTrade(
                signal_id=signal.signal_id,
                coin=signal.coin,
                direction=signal.direction,
                source_leverage=signal.source_leverage,
                source_return_pct=source_return_pct,
                source_liquidated=signal.liquidated,
                latency_ms=latency_ms,
                margin_usd=margin_usd,
                status="MISSED",
                entry_vwap=entry_price,
                exit_vwap=None,
                gross_return_pct=None,
                fee_drag_pct=None,
                net_return_pct=None,
                entry_book_age_ms=entry_target - entry_snapshot.timestamp_ms,
                exit_book_age_ms=exit_target - exit_snapshot.timestamp_ms,
                reason="EXIT_DEPTH_OR_SLIPPAGE",
            )
        entry_age = entry_target - entry_snapshot.timestamp_ms
        exit_age = exit_target - exit_snapshot.timestamp_ms

    sign = ONE if signal.direction == "LONG" else D("-1")
    gross_pnl = sign * quantity * (exit_price - entry_price)
    entry_fee = quantity * entry_price * taker_fee_rate
    exit_fee = quantity * exit_price * taker_fee_rate
    gross_return_pct = gross_pnl / margin_usd * HUNDRED
    fee_drag_pct = (entry_fee + exit_fee) / margin_usd * HUNDRED
    net_return_pct = gross_return_pct - fee_drag_pct

    return PercentReplayTrade(
        signal_id=signal.signal_id,
        coin=signal.coin,
        direction=signal.direction,
        source_leverage=signal.source_leverage,
        source_return_pct=source_return_pct,
        source_liquidated=signal.liquidated,
        latency_ms=latency_ms,
        margin_usd=margin_usd,
        status="EXECUTED",
        entry_vwap=entry_price,
        exit_vwap=exit_price,
        gross_return_pct=gross_return_pct,
        fee_drag_pct=fee_drag_pct,
        net_return_pct=net_return_pct,
        entry_book_age_ms=entry_age,
        exit_book_age_ms=exit_age,
    )


def summarize(
    signals: tuple[CopySignal, ...],
    rows: list[PercentReplayTrade],
    *,
    mode: str,
    latency_ms: int,
    margin_usd: Decimal,
) -> PercentReplaySummary:
    source_returns = [signal.source_leveraged_return * HUNDRED for signal in signals]
    source_wins = sum(value > ZERO for value in source_returns)
    executed = [row for row in rows if row.status == "EXECUTED"]
    gross_returns = [row.gross_return_pct for row in executed if row.gross_return_pct is not None]
    net_returns = [row.net_return_pct for row in executed if row.net_return_pct is not None]

    avg_gross = sum(gross_returns, ZERO) / D(len(gross_returns)) if gross_returns else None
    avg_net = sum(net_returns, ZERO) / D(len(net_returns)) if net_returns else None
    med_net = D(str(median(net_returns))) if net_returns else None
    net_wins = sum(value > ZERO for value in net_returns)
    net_win_rate = D(net_wins) / D(len(net_returns)) if net_returns else None
    source_avg = sum(source_returns, ZERO) / D(len(source_returns))
    retention = avg_net / source_avg if avg_net is not None and source_avg != ZERO else None

    return PercentReplaySummary(
        mode=mode,
        latency_ms=latency_ms,
        margin_usd=margin_usd,
        attempted=len(rows),
        executable=len(executed),
        missed=len(rows) - len(executed),
        source_liquidations=sum(signal.liquidated for signal in signals),
        source_avg_return_pct=source_avg,
        source_median_return_pct=D(str(median(source_returns))),
        source_win_rate=D(source_wins) / D(len(source_returns)),
        follower_avg_gross_return_pct=avg_gross,
        follower_avg_net_return_pct=avg_net,
        follower_median_net_return_pct=med_net,
        follower_net_win_rate=net_win_rate,
        avg_net_edge_retention=retention,
        execution_complete=len(executed) == len(rows),
    )


def run_percent_matrix(
    signals: tuple[CopySignal, ...],
    *,
    archive_dir: Path | None,
    latencies_ms: list[int],
    margins_usd: list[Decimal],
    taker_fee_bps: Decimal,
    max_slippage_bps: Decimal,
    max_book_age_ms: int,
    output_dir: Path,
) -> tuple[Path, Path]:
    provider = (
        LocalArchiveBookProvider(archive_dir, max_book_age_ms=max_book_age_ms)
        if archive_dir is not None
        else None
    )
    mode = "L2_EXECUTION" if provider is not None else "SOURCE_PRICE_BASELINE"
    summaries: list[PercentReplaySummary] = []
    all_rows: list[PercentReplayTrade] = []

    for latency_ms in latencies_ms:
        for margin_usd in margins_usd:
            rows = [
                replay_trade(
                    signal,
                    latency_ms=latency_ms,
                    margin_usd=margin_usd,
                    taker_fee_rate=taker_fee_bps / BPS,
                    max_slippage_bps=max_slippage_bps,
                    provider=provider,
                )
                for signal in signals
            ]
            summaries.append(
                summarize(
                    signals,
                    rows,
                    mode=mode,
                    latency_ms=latency_ms,
                    margin_usd=margin_usd,
                )
            )
            all_rows.extend(rows)

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"percent_copyability_{stamp}.json"
    csv_path = output_dir / f"percent_copyability_{stamp}.csv"
    payload = {
        "summaries": [summary.to_dict() for summary in summaries],
        "trades": [row.to_dict() for row in all_rows],
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    fields = list(summaries[0].to_dict()) if summaries else []
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if fields:
            writer.writeheader()
            writer.writerows(summary.to_dict() for summary in summaries)
    return json_path, csv_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hlcopy.copyability.percent_replay",
        description=(
            "Replay copy signals in per-trade percentage-return space using each source "
            "trade's actual leverage; no source portfolio balance is inferred."
        ),
    )
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--since")
    parser.add_argument("--coins", nargs="+")
    parser.add_argument("--directions", nargs="+", choices=["LONG", "SHORT"])
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument(
        "--latencies-ms",
        nargs="+",
        type=_nonnegative_int,
        default=[250, 500, 1000, 2000, 5000, 10000, 30000],
    )
    parser.add_argument(
        "--margin-usd-grid",
        nargs="+",
        type=_positive_decimal,
        default=[D("100"), D("500"), D("1000")],
        help="fixed follower margin per trade used only to test L2 capacity/slippage",
    )
    parser.add_argument("--taker-fee-bps", type=_positive_decimal, default=D("4.5"))
    parser.add_argument("--max-slippage-bps", type=_positive_decimal, default=D("20"))
    parser.add_argument("--max-book-age-ms", type=int, default=1000)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    imported = load_invo_closed_trades(
        args.csv,
        coins=set(args.coins) if args.coins else None,
        directions=set(args.directions) if args.directions else None,
        since_ms=_since_ms(args.since),
    )
    if not imported.signals:
        raise SystemExit("no signals matched")
    if args.archive_dir is None:
        print(
            "WARNING: source-price baseline only; this does not prove "
            "latency/slippage copyability.",
            flush=True,
        )
    json_path, csv_path = run_percent_matrix(
        imported.signals,
        archive_dir=args.archive_dir,
        latencies_ms=args.latencies_ms,
        margins_usd=args.margin_usd_grid,
        taker_fee_bps=args.taker_fee_bps,
        max_slippage_bps=args.max_slippage_bps,
        max_book_age_ms=args.max_book_age_ms,
        output_dir=args.output_dir,
    )
    print(f"loaded {len(imported.signals)} signals; malformed={len(imported.rejected_rows)}")
    print(f"wrote {json_path}")
    print(f"wrote {csv_path}")


if __name__ == "__main__":
    main()
