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


@dataclass(frozen=True, slots=True)
class SourceTruth:
    trades: int
    gross_winners: int
    gross_losers: int
    gross_breakeven: int
    gross_win_rate: Decimal
    weighted_gross_portfolio_return_sum: Decimal
    allocation_min: Decimal
    allocation_max: Decimal
    allocation_mean: Decimal
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        converted: dict[str, object] = {}
        for key, value in row.items():
            if isinstance(value, Decimal):
                converted[key] = str(value)
            elif isinstance(value, tuple):
                converted[key] = list(value)
            else:
                converted[key] = value
        return converted


def audit_source_truth(signals: tuple[CopySignal, ...]) -> SourceTruth:
    if not signals:
        raise ValueError("at least one signal is required")

    returns = [signal.source_leveraged_return for signal in signals]
    allocations = [signal.allocation_fraction for signal in signals]
    winners = sum(value > ZERO for value in returns)
    losers = sum(value < ZERO for value in returns)
    breakeven = len(returns) - winners - losers

    weighted = sum(
        signal.allocation_fraction * signal.source_leveraged_return
        for signal in signals
    )

    return SourceTruth(
        trades=len(signals),
        gross_winners=winners,
        gross_losers=losers,
        gross_breakeven=breakeven,
        gross_win_rate=D(winners) / D(len(signals)),
        weighted_gross_portfolio_return_sum=weighted,
        allocation_min=min(allocations),
        allocation_max=max(allocations),
        allocation_mean=sum(allocations) / D(len(allocations)),
        notes=(
            (
                "Gross trade P&L is reconstructed only from direction, entry price, "
                "exit price, and source leverage."
            ),
            (
                "Each trade is weighted by its own exported entry_size percentage; "
                "no equal-size assumption is used."
            ),
            (
                "entry_sim and last_sim are deliberately ignored because their "
                "currency/unit semantics are undocumented."
            ),
            (
                "No fees, latency, slippage, funding, or liquidation-path assumptions "
                "are included in source truth."
            ),
        ),
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
        prog="python -m hlcopy.copyability.source_truth",
        description="Audit source trades without undocumented simulation-field assumptions.",
    )
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--since")
    parser.add_argument("--coins", nargs="+")
    parser.add_argument("--directions", nargs="+", choices=["LONG", "SHORT"])
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
    truth = audit_source_truth(imported.signals)
    print(json.dumps(truth.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
