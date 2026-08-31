"""Read-only net-edge report for the Invo notification shadow ledger.

Research/reporting only. This command reads an append-only audit stream and writes
a report; it never mutates the shadow registry, never proposes an order and refuses
to run when real trading is enabled.
"""

from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from hlcopy.profitability.notification_edge import (
    DEFAULT_COST_SCENARIOS_BPS,
    DEFAULT_REFERENCE_COST_BPS,
    EdgePolicy,
    build_report,
    load_audit_rows,
    reconstruct_ledger,
)

D = Decimal


def _decimal_arg(value: str) -> Decimal:
    try:
        return D(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("value must be numeric") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hlcopy-notification-edge",
        description="Attribute realistic net copy edge across the Invo notification ledger",
    )
    parser.add_argument(
        "--audit",
        type=Path,
        default=Path("/var/lib/hyperliquid-copy-engine/invo-notification-executor/audit.jsonl"),
        help="append-only executor audit stream",
    )
    parser.add_argument("--output", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--min-closed-trades", type=int, default=30)
    parser.add_argument("--min-distinct-days", type=int, default=5)
    parser.add_argument(
        "--reference-cost-bps",
        type=_decimal_arg,
        default=DEFAULT_REFERENCE_COST_BPS,
        help="round-trip execution cost a slice must clear to be promotable",
    )
    parser.add_argument(
        "--cost-scenario-bps",
        type=_decimal_arg,
        action="append",
        default=None,
        help="repeatable round-trip cost scenario; defaults to 9/15/25/40",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="how many trader-coin-side slices to print",
    )
    return parser


def _opt(value: object) -> Decimal | None:
    return None if value is None else D(str(value))


def _fmt(value: object, digits: str = "0.01") -> str:
    if value is None:
        return "-"
    if isinstance(value, Decimal):
        return str(value.quantize(D(digits)))
    return str(value)


def _print_summary(report: dict[str, object], *, top: int, reference_cost: Decimal) -> None:
    integrity = report["integrity"]
    stale = report["stale_signals"]
    print("=== INVO NOTIFICATION NET EDGE ===")
    print(f"model={report['model_version']} real_trading={report['real_trading']}")
    print("integrity=" + json.dumps(integrity, sort_keys=True))
    print("stale_signals=" + json.dumps(stale, sort_keys=True))

    slices = report["slices"]
    assert isinstance(slices, list)
    by_dimension: dict[str, list[dict]] = {}
    for item in slices:
        by_dimension.setdefault(item["dimension"], []).append(item)

    for dimension in ("all", "signal_age", "trader", "coin", "trader_coin_side"):
        rows = by_dimension.get(dimension) or []
        if dimension == "trader_coin_side":
            rows = sorted(rows, key=lambda r: -r["closed_trades"])[:top]
        print(f"\n--- {dimension} ---")
        header = (
            f"{'key':<34} {'n':>4} {'open':>4} {'days':>4} "
            f"{'gross_bps':>10} {'ci_low':>9} {'breakeven':>10} "
            f"{f'net@{reference_cost}':>10} {'verdict':>22}"
        )
        print(header)
        for row in rows:
            net = row["net_by_cost_bps"].get(str(reference_cost)) or {}
            gross = _opt(row["mean_gross_return_bps"])
            ci_low = _opt(row["gross_return_ci_low_bps"])
            breakeven = _opt(row["breakeven_cost_bps"])
            net_bps = _opt(net.get("mean_net_return_bps"))
            print(
                f"{row['key'][:34]:<34} {row['closed_trades']:>4} "
                f"{row['open_trades']:>4} {row['distinct_days']:>4} "
                f"{_fmt(gross):>10} {_fmt(ci_low):>9} {_fmt(breakeven):>10} "
                f"{_fmt(net_bps):>10} {row['verdict']:>22}"
            )

    print(f"\neligible_slices={report['eligible_slice_count']}")
    for item in slices:
        if item["verdict"] == "ELIGIBLE_FOR_MICRO_LIVE":
            print(f"ELIGIBLE {item['dimension']}:{item['key']} n={item['closed_trades']}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("notification edge report refuses REAL_TRADING_ENABLED=YES")

    rows = load_audit_rows(args.audit)
    if not rows:
        raise SystemExit(f"NOTIFICATION_EDGE=NO_AUDIT_ROWS path={args.audit}")

    ledger = reconstruct_ledger(rows)
    policy = EdgePolicy(
        min_closed_trades=args.min_closed_trades,
        min_distinct_days=args.min_distinct_days,
        reference_cost_bps=args.reference_cost_bps,
    )
    scenarios = tuple(args.cost_scenario_bps or DEFAULT_COST_SCENARIOS_BPS)
    if args.reference_cost_bps not in scenarios:
        scenarios = tuple(sorted({*scenarios, args.reference_cost_bps}))
    report = build_report(ledger, policy=policy, cost_scenarios_bps=scenarios)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_suffix(args.output.suffix + ".tmp")
        tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(args.output)

    _print_summary(report, top=args.top, reference_cost=args.reference_cost_bps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
