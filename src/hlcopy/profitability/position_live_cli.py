from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hlcopy.profitability.portfolio_position_copy import simulate_copy_with_portfolio_capital
from hlcopy.profitability.position_copy import (
    CopyFillEvent,
    load_direct_events,
    load_wide_events,
)
from hlcopy.profitability.progress import Progress
from hlcopy.shadow.evaluator import ParquetL2BookProvider
from hlcopy.shadow.latency import LatencyScenario
from hlcopy.shadow.registry import WalletRegistry

D = Decimal
ZERO = D("0")
BPS = D("10000")

SCENARIOS = (
    LatencyScenario("LIVE_100MS", 20.0, 40.0, 40.0),
    LatencyScenario("LIVE_250MS", 50.0, 100.0, 100.0),
    LatencyScenario("LIVE_500MS", 100.0, 200.0, 200.0),
    LatencyScenario("LIVE_1000MS", 200.0, 400.0, 400.0),
)
NOTIONALS = (D("1000"), D("5000"), D("10000"), D("25000"), D("50000"))


def _summary(sim) -> dict[str, object]:
    net_pnl = sim.realized_gross_pnl_usd - sim.total_fees_usd
    realized = list(sim.realized_slices)
    wins = sum(item.net_pnl_usd > ZERO for item in realized)
    positive = [item.net_pnl_usd for item in realized if item.net_pnl_usd > ZERO]
    negative = [-item.net_pnl_usd for item in realized if item.net_pnl_usd < ZERO]
    return_bps = net_pnl / sim.notional_usd * BPS if sim.notional_usd > ZERO else ZERO
    return {
        "lane": sim.lane,
        "wallet_id": sim.wallet_id,
        "wallet_address": sim.wallet_address,
        "scenario": sim.scenario,
        "notional_usd": str(sim.notional_usd),
        "peak_concurrent_gross_notional_usd": str(
            sim.peak_concurrent_gross_notional_usd
        ),
        "capital_exposure_model": "CAUSAL_EVENT_TIME_PEAK_GROSS_V1",
        "signals_or_episodes": sim.leader_events,
        "leader_fill_events": sim.leader_events,
        "executed": len(realized),
        "realized_actions": len(realized),
        "executable_events": sim.executable_events,
        "missed_events": sim.missed_events,
        "copied_increase_events": sim.copied_increase_events,
        "execution_pct": (
            100.0 * sim.executable_events / sim.leader_events
            if sim.leader_events
            else 0.0
        ),
        "closed_net_pnl_usd": str(net_pnl),
        "realized_gross_pnl_usd": str(sim.realized_gross_pnl_usd),
        "total_fees_usd": str(sim.total_fees_usd),
        "net_return_bps": str(return_bps),
        "avg_net_pnl_usd": str(net_pnl / D(len(realized))) if realized else None,
        "avg_net_bps": str(return_bps / D(len(realized))) if realized else None,
        "median_net_bps": None,
        "win_pct": str(D(wins) / D(len(realized)) * D("100")) if realized else None,
        "profit_factor": (
            str(sum(positive, ZERO) / sum(negative, ZERO))
            if negative
            else ("Infinity" if positive else None)
        ),
        "max_closed_drawdown_usd": None,
        "p95_feed_ms": None,
        "open_positions": sim.open_positions,
        "evidence_tier": (
            "STRONG"
            if len(realized) >= 30
            else "DEVELOPING"
            if len(realized) >= 10
            else "EARLY"
        ),
        "pnl_model": "PROPORTIONAL_POSITION_CHANGE_V2",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hlcopy.profitability.position_live_cli"
    )
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


def _prime_provider(provider, events: tuple[CopyFillEvent, ...]) -> None:
    prime = getattr(provider, "prime", None)
    if callable(prime) and events:
        prime(events, SCENARIOS)


def main() -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("position profitability scorer refuses REAL_TRADING_ENABLED=YES")
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
    wide_events = load_wide_events(args.wide_enriched_dir, cutoff_ns=cutoff_ns)
    wide_by_wallet: dict[str, list[CopyFillEvent]] = defaultdict(list)
    for event in wide_events:
        wide_by_wallet[event.wallet_address].append(event)

    direct_by_wallet = {
        wallet.id: load_direct_events(args.shadow_dir, wallet.id)
        for wallet in direct_wallets
    }
    direct_events = tuple(
        event
        for wallet_events in direct_by_wallet.values()
        for event in wallet_events
    )

    print(
        f"profitability_start direct_wallets={len(direct_wallets)} "
        f"wide_wallets={len(wide_by_wallet)} wide_events={len(wide_events)} "
        f"scenarios={len(SCENARIOS)} notionals={len(NOTIONALS)}",
        flush=True,
    )

    if direct_provider is wide_provider:
        _prime_provider(direct_provider, direct_events + wide_events)
    else:
        _prime_provider(direct_provider, direct_events)
        _prime_provider(wide_provider, wide_events)

    summaries: list[dict[str, object]] = []
    slices: list[dict[str, object]] = []
    progress = Progress("profitability", every=10)
    for scenario in SCENARIOS:
        for notional in NOTIONALS:
            for wallet in direct_wallets:
                events = direct_by_wallet[wallet.id]
                if not events:
                    continue
                sim = simulate_copy_with_portfolio_capital(
                    events,
                    provider=direct_provider,
                    scenario=scenario,
                    notional_usd=notional,
                    taker_fee_bps=max(ZERO, args.taker_fee_bps),
                    max_slippage_bps=max(D("0.1"), args.max_slippage_bps),
                    max_book_forward_ms=max(1, args.max_book_forward_ms),
                )
                summaries.append(_summary(sim))
                slices.extend(
                    item.to_dict()
                    | {"scenario": scenario.name, "notional_usd": str(notional)}
                    for item in sim.realized_slices
                )
                progress.tick(
                    f"lane=DIRECT wallet={wallet.id} "
                    f"scenario={scenario.name} notional={notional}"
                )

            for address, events in wide_by_wallet.items():
                sim = simulate_copy_with_portfolio_capital(
                    events,
                    provider=wide_provider,
                    scenario=scenario,
                    notional_usd=notional,
                    taker_fee_bps=max(ZERO, args.taker_fee_bps),
                    max_slippage_bps=max(D("0.1"), args.max_slippage_bps),
                    max_book_forward_ms=max(1, args.max_book_forward_ms),
                )
                summaries.append(_summary(sim))
                slices.extend(
                    item.to_dict()
                    | {"scenario": scenario.name, "notional_usd": str(notional)}
                    for item in sim.realized_slices
                )
                progress.tick(
                    f"lane=WIDE wallet={address[:14]} "
                    f"scenario={scenario.name} notional={notional}"
                )

    summaries.sort(
        key=lambda row: (
            int(row["realized_actions"]),
            D(str(row["closed_net_pnl_usd"])),
        ),
        reverse=True,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "real_trading": False,
        "pnl_model": "PROPORTIONAL_POSITION_CHANGE_V2",
        "model_notes": (
            "Copies prospectively observed exposure increases. First increase on a "
            "legacy position copies only the observed fraction; subsequent deltas use "
            "a fixed leader:follower scale, capped at configured follower notional. "
            "Partial reductions realize PnL immediately. Peak concurrent gross "
            "notional is measured causally at wallet-event times for portfolio capital "
            "research; continuous MTM remains unmodeled."
        ),
        "funding_mode": "NOT_MODELED_YET",
        "liquidation_path_mode": "NOT_MODELED_YET",
        "open_position_mark_to_market": "NOT_INCLUDED_YET",
        "capital_exposure_model": "CAUSAL_EVENT_TIME_PEAK_GROSS_V1",
        "summaries": summaries,
        "realized_slices": slices,
    }
    json_path = args.output_dir / "master_profitability.json"
    csv_path = args.output_dir / "master_profitability.csv"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fields = list(summaries[0]) if summaries else []
        writer = csv.DictWriter(handle, fieldnames=fields)
        if fields:
            writer.writeheader()
            writer.writerows(summaries)
    print(
        f"position profitability rows={len(summaries)} "
        f"direct_wallets={len(direct_wallets)} "
        f"wide_wallets={len(wide_by_wallet)} wide_events={len(wide_events)}"
    )
    nonempty = [row for row in summaries if int(row["realized_actions"]) > 0]
    print(f"rows_with_realized_pnl={len(nonempty)}")
    for row in nonempty[:20]:
        print(
            f"{row['lane']:<6} {str(row['wallet_address'])[:12]:<12} "
            f"{row['scenario']:<11} ${row['notional_usd']:<7} "
            f"closes={row['realized_actions']:<3} net=${row['closed_net_pnl_usd']} "
            f"return_bps={row['net_return_bps']} exec={row['execution_pct']:.1f}% "
            f"peak_gross=${row['peak_concurrent_gross_notional_usd']}"
        )


if __name__ == "__main__":
    main()
