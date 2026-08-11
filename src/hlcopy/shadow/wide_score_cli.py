from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hlcopy.shadow.evaluator import ParquetL2BookProvider
from hlcopy.shadow.latency import LatencyScenario
from hlcopy.shadow.wide_score import (
    WideScoreConfig,
    build_wide_episodes,
    load_wide_signals,
    score_wide_episode,
    wallet_summary,
)

D = Decimal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.shadow.wide_score_cli")
    parser.add_argument("--enriched-dir", required=True, type=Path)
    parser.add_argument("--cutoff-ns-file", required=True, type=Path)
    parser.add_argument("--market-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--notional-usd", type=Decimal, default=D("1000"))
    parser.add_argument("--taker-fee-bps", type=Decimal, default=D("4.5"))
    parser.add_argument("--max-slippage-bps", type=Decimal, default=D("20"))
    parser.add_argument("--max-book-forward-ms", type=int, default=750)
    parser.add_argument("--decision-ms", type=float, default=50.0)
    parser.add_argument("--outbound-ms", type=float, default=100.0)
    parser.add_argument("--exchange-ms", type=float, default=100.0)
    return parser


def _fmt(value: object, width: int = 9) -> str:
    if value is None:
        return "-".rjust(width)
    if isinstance(value, Decimal):
        return f"{float(value):+.2f}".rjust(width)
    if isinstance(value, float):
        return f"{value:.1f}".rjust(width)
    return str(value).rjust(width)


def main() -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("wide score refuses REAL_TRADING_ENABLED=YES")
    args = build_parser().parse_args()
    cutoff_ns = int(args.cutoff_ns_file.read_text(encoding="utf-8").strip())
    signals = load_wide_signals(args.enriched_dir, cutoff_ns=cutoff_ns)
    episodes = build_wide_episodes(signals)
    provider = ParquetL2BookProvider(args.market_dir)
    scenario = LatencyScenario(
        "LIVE_250MS",
        decision_ms=max(0.0, args.decision_ms),
        outbound_order_ms=max(0.0, args.outbound_ms),
        exchange_processing_ms=max(0.0, args.exchange_ms),
    )
    config = WideScoreConfig(
        notional_usd=max(D("1"), args.notional_usd),
        taker_fee_bps=max(D("0"), args.taker_fee_bps),
        max_slippage_bps=max(D("0.1"), args.max_slippage_bps),
        max_book_forward_ms=max(1, args.max_book_forward_ms),
    )
    now_ms = int(time.time() * 1000)
    scores = [
        score_wide_episode(
            episode,
            provider=provider,
            scenario=scenario,
            config=config,
            now_ms=now_ms,
        )
        for episode in episodes
    ]
    summaries = wallet_summary(scores)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "cutoff_ns": cutoff_ns,
        "scenario": {
            "name": scenario.name,
            "decision_ms": scenario.decision_ms,
            "outbound_order_ms": scenario.outbound_order_ms,
            "exchange_processing_ms": scenario.exchange_processing_ms,
        },
        "config": {
            "notional_usd": str(config.notional_usd),
            "taker_fee_bps": str(config.taker_fee_bps),
            "max_slippage_bps": str(config.max_slippage_bps),
            "max_book_forward_ms": config.max_book_forward_ms,
            "horizons_minutes": list(config.horizons_minutes),
        },
        "safety": {
            "funding_mode": "NOT_MODELED",
            "liquidation_path_mode": "NOT_MODELED",
            "real_trading": False,
        },
        "summary": summaries,
        "episodes": [score.to_dict() for score in scores],
    }
    path = args.output_dir / "wide_live_scoreboard.json"
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")

    print("=" * 132)
    print(" LIVE EXECUTION SCOREBOARD — NET BPS AFTER L2 VWAP + TAKER FEES")
    print("=" * 132)
    print(
        f"{'TIME':<20} {'WALLET':<12} {'COIN':<12} {'DIR':<6} {'FEED':>7} "
        f"{'SLIP':>9} {'1M':>9} {'5M':>9} {'15M':>9} {'60M':>9} "
        f"{'CLOSED':>9} {'STATUS':<18}"
    )
    print("-" * 132)
    for score in scores:
        when = datetime.fromtimestamp(score.source_entry_ts_ms / 1000, UTC).strftime(
            "%m-%d %H:%M:%S"
        )
        marks = score.markouts_net_bps
        print(
            f"{when:<20} {score.wallet_address[:10]:<12} {score.coin:<12} "
            f"{score.direction:<6} {score.feed_ms:>7.1f} "
            f"{_fmt(score.entry_slippage_bps)} {_fmt(marks.get('1m'))} "
            f"{_fmt(marks.get('5m'))} {_fmt(marks.get('15m'))} "
            f"{_fmt(marks.get('60m'))} {_fmt(score.closed_net_bps)} "
            f"{score.status:<18}"
        )
        if score.reason:
            print(f"  reason: {score.reason}")

    print()
    print("=" * 132)
    print(" WALLET SCORECARD")
    print("=" * 132)
    print(
        f"{'WALLET':<44} {'SIG':>4} {'EXEC':>5} {'EXEC%':>7} {'AVG5M':>9} "
        f"{'MED5M':>9} {'CLOSED':>7} {'AVGCLOSE':>10} {'WIN%':>7} {'P95FEED':>9}"
    )
    print("-" * 132)
    for row in summaries:
        print(
            f"{str(row['wallet']):<44} {int(row['signals']):>4} "
            f"{int(row['executable']):>5} {float(row['execution_pct']):>6.1f}% "
            f"{_fmt(row['avg_5m_net_bps'])} {_fmt(row['median_5m_net_bps'])} "
            f"{int(row['closed']):>7} {_fmt(row['avg_closed_net_bps'], 10)} "
            f"{_fmt(row['closed_win_pct'], 7)} {_fmt(row['p95_feed_ms'])}"
        )

    print()
    print("Funding: NOT MODELED | Liquidation path: NOT MODELED | Approval: BLOCKED")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
