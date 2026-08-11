from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

from hlcopy.shadow.evaluator import (
    ExecutionConfig,
    ParquetL2BookProvider,
    evaluate_episode,
    load_prospective_episodes,
)
from hlcopy.shadow.latency import LatencyScenario
from hlcopy.shadow.registry import WalletRegistry
from hlcopy.shadow.wide_score import (
    WideScoreConfig,
    build_wide_episodes,
    load_wide_signals,
    score_wide_episode,
)

D = Decimal
ZERO = D("0")
BPS = D("10000")

DEFAULT_SCENARIOS = (
    LatencyScenario("LIVE_100MS", 20.0, 40.0, 40.0),
    LatencyScenario("LIVE_250MS", 50.0, 100.0, 100.0),
    LatencyScenario("LIVE_500MS", 100.0, 200.0, 200.0),
    LatencyScenario("LIVE_1000MS", 200.0, 400.0, 400.0),
)
DEFAULT_NOTIONALS = (D("1000"), D("5000"), D("10000"), D("25000"), D("50000"))


def _percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * probability)))
    return ordered[index]


def _summary(
    *,
    lane: str,
    wallet_id: str,
    wallet_address: str,
    scenario: str,
    notional: Decimal,
    rows: list[dict[str, object]],
) -> dict[str, object]:
    executed = [row for row in rows if row.get("net_bps") is not None]
    net_bps = [D(str(row["net_bps"])) for row in executed]
    pnls = [value / BPS * notional for value in net_bps]
    wins = sum(value > ZERO for value in pnls)
    losses = [-value for value in pnls if value < ZERO]
    gains = [value for value in pnls if value > ZERO]
    cumulative = ZERO
    peak = ZERO
    max_dd = ZERO
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    feed = [float(row["feed_ms"]) for row in rows if row.get("feed_ms") is not None]
    return {
        "lane": lane,
        "wallet_id": wallet_id,
        "wallet_address": wallet_address,
        "scenario": scenario,
        "notional_usd": str(notional),
        "signals_or_episodes": len(rows),
        "executed": len(executed),
        "execution_pct": (100.0 * len(executed) / len(rows) if rows else 0.0),
        "closed_net_pnl_usd": str(sum(pnls, ZERO)),
        "avg_net_pnl_usd": str(sum(pnls, ZERO) / D(len(pnls))) if pnls else None,
        "avg_net_bps": str(sum(net_bps, ZERO) / D(len(net_bps))) if net_bps else None,
        "median_net_bps": str(D(str(median(net_bps)))) if net_bps else None,
        "win_pct": str(D(wins) / D(len(pnls)) * D("100")) if pnls else None,
        "profit_factor": (
            str(sum(gains, ZERO) / sum(losses, ZERO)) if losses else ("Infinity" if gains else None)
        ),
        "max_closed_drawdown_usd": str(max_dd),
        "p95_feed_ms": _percentile(feed, 0.95),
        "evidence_tier": (
            "STRONG" if len(executed) >= 30 else "DEVELOPING" if len(executed) >= 10 else "EARLY"
        ),
    }


def _substrategy(rows: list[dict[str, object]], notional: Decimal) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("coin")), str(row.get("direction")))].append(row)
    out: list[dict[str, object]] = []
    for (coin, direction), items in grouped.items():
        net = [D(str(row["net_bps"])) for row in items if row.get("net_bps") is not None]
        if not net:
            continue
        out.append(
            {
                "coin": coin,
                "direction": direction,
                "executed": len(net),
                "net_pnl_usd": str(sum((value / BPS * notional for value in net), ZERO)),
                "avg_net_bps": str(sum(net, ZERO) / D(len(net))),
                "win_pct": str(D(sum(value > ZERO for value in net)) / D(len(net)) * D("100")),
            }
        )
    out.sort(key=lambda row: D(str(row["net_pnl_usd"])), reverse=True)
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.profitability.live_cli")
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--shadow-dir", required=True, type=Path)
    parser.add_argument("--shadow-market-dir", required=True, type=Path)
    parser.add_argument("--wide-enriched-dir", required=True, type=Path)
    parser.add_argument("--wide-cutoff-ns-file", required=True, type=Path)
    parser.add_argument("--wide-market-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--taker-fee-bps", type=Decimal, default=D("4.5"))
    parser.add_argument("--max-slippage-bps", type=Decimal, default=D("20"))
    parser.add_argument("--max-book-forward-ms", type=int, default=750)
    return parser


def main() -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("profitability scorer refuses REAL_TRADING_ENABLED=YES")
    args = build_parser().parse_args()
    registry = WalletRegistry(args.registry)
    direct_wallets = tuple(
        wallet
        for wallet in registry.load()
        if wallet.enabled
        and wallet.source_type == "hyperliquid_wallet"
        and wallet.stage in {"validation", "approved"}
    )
    direct_provider = ParquetL2BookProvider(args.shadow_market_dir)
    wide_provider = ParquetL2BookProvider(args.wide_market_dir)
    cutoff_ns = int(args.wide_cutoff_ns_file.read_text(encoding="utf-8").strip())
    wide_episodes = build_wide_episodes(load_wide_signals(args.wide_enriched_dir, cutoff_ns=cutoff_ns))
    now_ms = int(time.time() * 1000)

    summaries: list[dict[str, object]] = []
    substrategies: list[dict[str, object]] = []

    for wallet in direct_wallets:
        episodes = load_prospective_episodes(args.shadow_dir, wallet.id)
        for scenario in DEFAULT_SCENARIOS:
            for notional in DEFAULT_NOTIONALS:
                config = ExecutionConfig(
                    notional_usd=notional,
                    follower_leverage=D("1"),
                    taker_fee_bps=max(ZERO, args.taker_fee_bps),
                    max_slippage_bps=max(D("0.1"), args.max_slippage_bps),
                    max_book_forward_ms=max(1, args.max_book_forward_ms),
                )
                rows: list[dict[str, object]] = []
                for episode in episodes:
                    result = evaluate_episode(episode, provider=direct_provider, scenario=scenario, config=config)
                    rows.append(
                        {
                            "coin": result.coin,
                            "direction": result.direction,
                            "feed_ms": result.entry_signal_feed_ms,
                            "net_bps": result.net_underlying_bps,
                        }
                    )
                summary = _summary(
                    lane="DIRECT",
                    wallet_id=wallet.id,
                    wallet_address=wallet.source_ref.lower(),
                    scenario=scenario.name,
                    notional=notional,
                    rows=rows,
                )
                summaries.append(summary)
                substrategies.append(summary | {"breakdown": _substrategy(rows, notional)})

    for scenario in DEFAULT_SCENARIOS:
        for notional in DEFAULT_NOTIONALS:
            config = WideScoreConfig(
                notional_usd=notional,
                taker_fee_bps=max(ZERO, args.taker_fee_bps),
                max_slippage_bps=max(D("0.1"), args.max_slippage_bps),
                max_book_forward_ms=max(1, args.max_book_forward_ms),
            )
            by_wallet: dict[str, list[dict[str, object]]] = defaultdict(list)
            wallet_ids: dict[str, str] = {}
            for episode in wide_episodes:
                score = score_wide_episode(
                    episode,
                    provider=wide_provider,
                    scenario=scenario,
                    config=config,
                    now_ms=now_ms,
                )
                wallet_ids[score.wallet_address] = score.wallet_id
                by_wallet[score.wallet_address].append(
                    {
                        "coin": score.coin,
                        "direction": score.direction,
                        "feed_ms": score.feed_ms,
                        "net_bps": score.closed_net_bps,
                    }
                )
            for address, rows in by_wallet.items():
                summary = _summary(
                    lane="WIDE",
                    wallet_id=wallet_ids[address],
                    wallet_address=address,
                    scenario=scenario.name,
                    notional=notional,
                    rows=rows,
                )
                summaries.append(summary)
                substrategies.append(summary | {"breakdown": _substrategy(rows, notional)})

    def rank_key(row: dict[str, object]) -> tuple[int, Decimal, Decimal]:
        executed = int(row["executed"])
        pnl = D(str(row["closed_net_pnl_usd"]))
        avg = D(str(row["avg_net_bps"])) if row["avg_net_bps"] is not None else D("-Infinity")
        return (executed, pnl, avg)

    ranked = sorted(summaries, key=rank_key, reverse=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "real_trading": False,
        "funding_mode": "NOT_MODELED_YET",
        "liquidation_path_mode": "NOT_MODELED_YET",
        "ranking_warning": "Dollar PnL is fixed-notional episode PnL, not a compounded portfolio equity curve.",
        "summaries": ranked,
        "substrategies": substrategies,
    }
    json_path = args.output_dir / "master_profitability.json"
    csv_path = args.output_dir / "master_profitability.csv"
    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fields = list(ranked[0]) if ranked else []
        writer = csv.DictWriter(handle, fieldnames=fields)
        if fields:
            writer.writeheader()
            writer.writerows(ranked)
    print(f"profitability rows={len(ranked)} direct_wallets={len(direct_wallets)} wide_episodes={len(wide_episodes)}")
    for row in ranked[:20]:
        print(
            f"{row['lane']:<6} {str(row['wallet_address'])[:12]:<12} {row['scenario']:<11} "
            f"${row['notional_usd']:<7} n={row['executed']:<3} pnl=${row['closed_net_pnl_usd']} "
            f"avg_bps={row['avg_net_bps']} win={row['win_pct']} tier={row['evidence_tier']}"
        )
    print(f"wrote {json_path}")
    print(f"wrote {csv_path}")


if __name__ == "__main__":
    main()
