from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hlcopy.signals.invo import CopySignal, load_invo_closed_trades

D = Decimal
ZERO = D("0")
BPS = D("10000")
DEFAULT_STARTING_CAPITAL = D("10000")
DEFAULT_TAKER_FEE_BPS = D("4.5")
DEFAULT_SIM_MATCH_TOLERANCE = D("0.000001")


@dataclass(frozen=True, slots=True)
class MirrorResult:
    starting_capital: Decimal
    ending_capital: Decimal
    roi: Decimal
    win_rate: Decimal
    copied: int
    max_drawdown: Decimal
    fee_rate_per_side: Decimal
    sizing_mode: str = "REALIZED_EQUITY_ONLY"
    leverage_mode: str = "SOURCE_PER_TRADE"

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        for key, value in list(row.items()):
            if isinstance(value, Decimal):
                row[key] = str(value)
        return row


@dataclass(frozen=True, slots=True)
class SourceAudit:
    signals: int
    winners: int
    losers: int
    breakeven: int
    source_win_rate: Decimal
    weighted_gross_return_sum: Decimal
    simulated_return_observations: int
    simulated_return_matches: int
    simulated_return_mismatches: int
    first_implied_equity: Decimal | None
    last_implied_equity: Decimal | None
    implied_equity_change: Decimal | None
    max_concurrent_source_allocation: Decimal
    gross_realized_only_mirror: MirrorResult
    fee_adjusted_realized_only_mirror: MirrorResult
    mismatch_rows: tuple[dict[str, str], ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)

        def convert(value: object) -> object:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [convert(item) for item in value]
            return value

        return convert(row)  # type: ignore[return-value]


def _mirror(
    signals: tuple[CopySignal, ...],
    *,
    starting_capital: Decimal,
    fee_rate_per_side: Decimal,
) -> MirrorResult:
    events: list[tuple[int, int, str]] = []
    by_id = {signal.signal_id: signal for signal in signals}
    for signal in signals:
        events.append((signal.closed_at_ms, 0, signal.signal_id))
        events.append((signal.opened_at_ms, 1, signal.signal_id))
    events.sort()

    equity = starting_capital
    peak = equity
    max_drawdown = ZERO
    open_positions: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    pnls: list[Decimal] = []

    for _timestamp, event_kind, signal_id in events:
        signal = by_id[signal_id]
        if event_kind == 1:
            margin = equity * signal.allocation_fraction
            notional = margin * signal.source_leverage
            quantity = notional / signal.entry_price
            entry_fee = notional * fee_rate_per_side
            equity -= entry_fee
            open_positions[signal_id] = (
                quantity,
                signal.entry_price,
                entry_fee,
                signal.allocation_fraction,
            )
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity - peak)
            continue

        position = open_positions.pop(signal_id, None)
        if position is None:
            continue
        quantity, entry_price, entry_fee, _allocation = position
        sign = D("1") if signal.direction == "LONG" else D("-1")
        gross = sign * quantity * (signal.exit_price - entry_price)
        exit_notional = quantity * signal.exit_price
        exit_fee = exit_notional * fee_rate_per_side
        net = gross - entry_fee - exit_fee
        equity += gross - exit_fee
        pnls.append(net)
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)

    wins = sum(value > ZERO for value in pnls)
    return MirrorResult(
        starting_capital=starting_capital,
        ending_capital=equity,
        roi=equity / starting_capital - D("1"),
        win_rate=D(wins) / D(len(pnls)) if pnls else ZERO,
        copied=len(pnls),
        max_drawdown=max_drawdown,
        fee_rate_per_side=fee_rate_per_side,
    )


def _max_concurrent_allocation(signals: tuple[CopySignal, ...]) -> Decimal:
    events: list[tuple[int, int, Decimal]] = []
    for signal in signals:
        events.append((signal.closed_at_ms, 0, signal.allocation_fraction))
        events.append((signal.opened_at_ms, 1, signal.allocation_fraction))
    events.sort()
    current = ZERO
    maximum = ZERO
    for _timestamp, kind, allocation in events:
        if kind == 0:
            current -= allocation
        else:
            current += allocation
            maximum = max(maximum, current)
    return maximum


def audit_signals(
    signals: tuple[CopySignal, ...],
    *,
    starting_capital: Decimal = DEFAULT_STARTING_CAPITAL,
    follower_taker_fee_bps: Decimal = DEFAULT_TAKER_FEE_BPS,
    sim_match_tolerance: Decimal = DEFAULT_SIM_MATCH_TOLERANCE,
) -> SourceAudit:
    if not signals:
        raise ValueError("at least one signal is required")

    ordered = tuple(sorted(signals, key=lambda item: (item.opened_at_ms, item.signal_id)))
    returns = [signal.source_leveraged_return for signal in ordered]
    winners = sum(value > ZERO for value in returns)
    losers = sum(value < ZERO for value in returns)
    breakeven = len(returns) - winners - losers

    mismatch_rows: list[dict[str, str]] = []
    sim_observations = 0
    sim_matches = 0
    implied_equities: list[tuple[int, Decimal]] = []

    for signal in ordered:
        if signal.entry_sim is not None and signal.allocation_fraction > ZERO:
            implied_equities.append(
                (signal.opened_at_ms, signal.entry_sim / signal.allocation_fraction)
            )
        if (
            signal.entry_sim is None
            or signal.last_sim is None
            or signal.entry_sim <= ZERO
        ):
            continue
        sim_observations += 1
        simulated_return = signal.last_sim / signal.entry_sim - D("1")
        difference = simulated_return - signal.source_leveraged_return
        if abs(difference) <= sim_match_tolerance:
            sim_matches += 1
        else:
            mismatch_rows.append(
                {
                    "signal_id": signal.signal_id,
                    "coin": signal.coin,
                    "direction": signal.direction,
                    "source_leverage": str(signal.source_leverage),
                    "price_formula_return": str(signal.source_leveraged_return),
                    "entry_sim_last_sim_return": str(simulated_return),
                    "difference": str(difference),
                }
            )

    first_implied: Decimal | None = None
    last_implied: Decimal | None = None
    implied_change: Decimal | None = None
    if implied_equities:
        implied_equities.sort(key=lambda item: item[0])
        first_implied = implied_equities[0][1]
        last_implied = implied_equities[-1][1]
        if first_implied > ZERO:
            implied_change = last_implied / first_implied - D("1")

    gross_mirror = _mirror(
        ordered,
        starting_capital=starting_capital,
        fee_rate_per_side=ZERO,
    )
    fee_mirror = _mirror(
        ordered,
        starting_capital=starting_capital,
        fee_rate_per_side=follower_taker_fee_bps / BPS,
    )

    notes = (
        (
            "Trade-card return is reconstructed as signed entry-to-exit price return "
            "times source leverage."
        ),
        (
            "entry_sim / allocation_fraction is treated only as an observed implied "
            "source-equity diagnostic."
        ),
        (
            "The mirror deliberately sizes from realized follower equity only; it does "
            "not mark open positions to market."
        ),
        (
            "Therefore the mirror is not expected to reproduce Invo lifetime portfolio "
            "P&L when overlapping unrealized P&L changes later trade sizing."
        ),
        (
            "Fee-adjusted mirror uses the supplied follower taker fee on both entry and "
            "exit; source Invo simulation values are not assumed to include those fees."
        ),
    )

    return SourceAudit(
        signals=len(ordered),
        winners=winners,
        losers=losers,
        breakeven=breakeven,
        source_win_rate=D(winners) / D(len(ordered)),
        weighted_gross_return_sum=sum(
            signal.allocation_fraction * signal.source_leveraged_return
            for signal in ordered
        ),
        simulated_return_observations=sim_observations,
        simulated_return_matches=sim_matches,
        simulated_return_mismatches=sim_observations - sim_matches,
        first_implied_equity=first_implied,
        last_implied_equity=last_implied,
        implied_equity_change=implied_change,
        max_concurrent_source_allocation=_max_concurrent_allocation(ordered),
        gross_realized_only_mirror=gross_mirror,
        fee_adjusted_realized_only_mirror=fee_mirror,
        mismatch_rows=tuple(mismatch_rows),
        notes=notes,
    )


def _since_ms(value: str | None) -> int | None:
    if not value:
        return None
    if "T" in value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hlcopy.copyability.source_audit",
        description="Reconcile Invo source-return semantics before copyability testing.",
    )
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--since")
    parser.add_argument("--coins", nargs="+")
    parser.add_argument("--directions", nargs="+", choices=["LONG", "SHORT"])
    parser.add_argument("--capital", type=Decimal, default=DEFAULT_STARTING_CAPITAL)
    parser.add_argument("--taker-fee-bps", type=Decimal, default=DEFAULT_TAKER_FEE_BPS)
    parser.add_argument("--json-out", type=Path)
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
    audit = audit_signals(
        imported.signals,
        starting_capital=args.capital,
        follower_taker_fee_bps=args.taker_fee_bps,
    )
    payload = audit.to_dict()
    print(json.dumps(payload, indent=2, sort_keys=True))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
