from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from hlcopy.shadow.evaluator import (
    ExecutionConfig,
    ParquetL2BookProvider,
    evaluate_episode,
    load_prospective_episodes,
    summarize_executions,
)
from hlcopy.shadow.latency import LatencyScenario
from hlcopy.shadow.manifest import fingerprint

D = Decimal


def _positive_decimal(value: str) -> Decimal:
    try:
        parsed = D(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("value must be numeric") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_decimal(value: str) -> Decimal:
    try:
        parsed = D(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("value must be numeric") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("value cannot be negative")
    return parsed


def _scenario(value: str) -> LatencyScenario:
    parts = value.split(":")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "scenario must be NAME:DECISION_MS:OUTBOUND_ORDER_MS:EXCHANGE_PROCESSING_MS"
        )
    name = parts[0].strip()
    try:
        decision, outbound, exchange = map(float, parts[1:])
        return LatencyScenario(name, decision, outbound, exchange)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("scenario latency values must be numeric") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.shadow.evaluate_cli")
    parser.add_argument("--wallet-id", required=True)
    parser.add_argument("--shadow-dir", required=True, type=Path)
    parser.add_argument("--market-dir", required=True, type=Path)
    parser.add_argument(
        "--scenario",
        action="append",
        type=_scenario,
        required=True,
        help="repeatable NAME:decision_ms:outbound_ms:exchange_processing_ms",
    )
    parser.add_argument(
        "--notional-usd-grid",
        nargs="+",
        type=_positive_decimal,
        default=[D("1000")],
    )
    parser.add_argument(
        "--leverage-grid",
        nargs="+",
        type=_positive_decimal,
        default=[D("1")],
    )
    parser.add_argument("--taker-fee-bps", type=_nonnegative_decimal, default=D("4.5"))
    parser.add_argument("--max-slippage-bps", type=_positive_decimal, default=D("20"))
    parser.add_argument("--max-book-forward-ms", type=int, default=750)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    episodes = load_prospective_episodes(args.shadow_dir, args.wallet_id)
    if not episodes:
        raise SystemExit(f"no completed prospective episodes for wallet {args.wallet_id}")
    provider = ParquetL2BookProvider(args.market_dir)
    summaries = []
    trades: list[dict[str, object]] = []
    for scenario in args.scenario:
        for notional in args.notional_usd_grid:
            for leverage in args.leverage_grid:
                config = ExecutionConfig(
                    notional_usd=notional,
                    follower_leverage=leverage,
                    taker_fee_bps=args.taker_fee_bps,
                    max_slippage_bps=args.max_slippage_bps,
                    max_book_forward_ms=args.max_book_forward_ms,
                )
                rows = [
                    evaluate_episode(
                        episode,
                        provider=provider,
                        scenario=scenario,
                        config=config,
                    )
                    for episode in episodes
                ]
                summary = summarize_executions(args.wallet_id, scenario, config, rows)
                summaries.append(summary.to_dict())
                for row in rows:
                    trades.append(
                        row.to_dict()
                        | {
                            "scenario_name": scenario.name,
                            "decision_ms": scenario.decision_ms,
                            "outbound_order_ms": scenario.outbound_order_ms,
                            "exchange_processing_ms": scenario.exchange_processing_ms,
                            "notional_usd": str(notional),
                            "follower_leverage": str(leverage),
                        }
                    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "wallet_id": args.wallet_id,
        "completed_source_episodes": len(episodes),
        "funding_mode": "NOT_MODELED",
        "liquidation_path_mode": "NOT_MODELED",
        "summaries": summaries,
        "trades": trades,
    }
    payload["evidence_fingerprint"] = fingerprint(payload)
    json_path = args.output_dir / f"shadow_evaluation_{args.wallet_id}_{stamp}.json"
    csv_path = args.output_dir / f"shadow_evaluation_{args.wallet_id}_{stamp}.csv"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = list(summaries[0]) if summaries else []
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(summaries)
    print(
        f"evaluated wallet={args.wallet_id} completed_source_episodes={len(episodes)} "
        f"matrix_rows={len(summaries)} fingerprint={payload['evidence_fingerprint'][:16]}",
        flush=True,
    )
    print(f"wrote {json_path}", flush=True)
    print(f"wrote {csv_path}", flush=True)


if __name__ == "__main__":
    main()
