from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hlcopy.profitability.causal_book import CausalParquetL2BookProvider
from hlcopy.profitability.lane1_handoff import (
    LANE1_SELECTION_CONTRACT_V1,
    build_challenger_queue,
)
from hlcopy.profitability.portfolio_position_copy import simulate_copy_with_portfolio_capital
from hlcopy.profitability.position_copy import CopyFillEvent, load_wide_events
from hlcopy.profitability.position_live_cli import NOTIONALS, SCENARIOS, _summary

D = Decimal
ZERO = D("0")
SCREEN_SCENARIO = SCENARIOS[2]  # LIVE_500MS
SCREEN_NOTIONAL = D("5000")
DEFAULT_UNIVERSE_STATE = Path(
    "/mnt/HC_Volume_106576526/hyperliquid/discovery/universe_state.json"
)


def _append_jsonl(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def _cohort_key(wallet: str, coin: str) -> str:
    return f"{wallet.lower()}|{coin}"


def _confirmation_key(wallet: str, coin: str, scenario: str, notional: str) -> str:
    return f"{_cohort_key(wallet, coin)}|{scenario}|{notional}"


def _simulate(
    events: tuple[CopyFillEvent, ...],
    *,
    market_dir: Path,
    scenario,
    notional: Decimal,
    taker_fee_bps: Decimal,
    max_slippage_bps: Decimal,
    max_book_forward_ms: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    provider = CausalParquetL2BookProvider(market_dir)
    provider.prime(events, (scenario,))
    sim = simulate_copy_with_portfolio_capital(
        events,
        provider=provider,
        scenario=scenario,
        notional_usd=notional,
        taker_fee_bps=max(ZERO, taker_fee_bps),
        max_slippage_bps=max(D("0.1"), max_slippage_bps),
        max_book_forward_ms=max(1, max_book_forward_ms),
    )
    summary = _summary(sim)
    slices = [
        item.to_dict()
        | {
            "scenario": scenario.name,
            "notional_usd": str(notional),
        }
        for item in sim.realized_slices
    ]
    return summary, slices


def _screen_rank(row: dict[str, object]) -> tuple[Decimal, int, Decimal]:
    return (
        D(str(row.get("net_return_bps") or "0")),
        int(row.get("realized_actions") or 0),
        D(str(row.get("closed_net_pnl_usd") or "0")),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hlcopy.profitability.incremental_funnel_cli"
    )
    parser.add_argument("--wide-enriched-dir", required=True, type=Path)
    parser.add_argument("--wide-cutoff-ns-file", required=True, type=Path)
    parser.add_argument("--market-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--min-events", type=int, default=6)
    parser.add_argument("--screen-limit", type=int, default=0)
    parser.add_argument("--confirm-top", type=int, default=40)
    parser.add_argument("--min-screen-actions", type=int, default=3)
    parser.add_argument("--taker-fee-bps", type=Decimal, default=D("4.5"))
    parser.add_argument("--max-slippage-bps", type=Decimal, default=D("20"))
    parser.add_argument("--max-book-forward-ms", type=int, default=750)
    parser.add_argument("--universe-state", type=Path, default=DEFAULT_UNIVERSE_STATE)
    parser.add_argument("--max-universe-age-hours", type=float, default=6.0)
    return parser


def main() -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("incremental profitability funnel refuses REAL_TRADING_ENABLED=YES")

    args = build_parser().parse_args()
    cutoff_ns = int(args.wide_cutoff_ns_file.read_text(encoding="utf-8").strip())
    events = load_wide_events(args.wide_enriched_dir, cutoff_ns=cutoff_ns)

    universe_payload: dict[str, object] = {}
    if args.universe_state is not None and args.universe_state.exists():
        universe_payload = json.loads(args.universe_state.read_text(encoding="utf-8"))

    grouped: dict[tuple[str, str], list[CopyFillEvent]] = defaultdict(list)
    for event in events:
        grouped[(event.wallet_address.lower(), event.coin)].append(event)

    cohorts = [
        (wallet, coin, tuple(rows))
        for (wallet, coin), rows in grouped.items()
        if len(rows) >= max(1, args.min_events)
    ]
    cohorts.sort(key=lambda item: len(item[2]), reverse=True)
    if args.screen_limit > 0:
        cohorts = cohorts[: args.screen_limit]

    screen_path = args.output_dir / "screening.jsonl"
    confirm_path = args.output_dir / "confirmation.jsonl"
    slice_path = args.output_dir / "realized_slices.jsonl"
    report_path = args.output_dir / "funnel_report.json"

    screened = _load_jsonl(screen_path)
    screened_keys = {
        _cohort_key(str(row["wallet_address"]), str(row["coin"])) for row in screened
    }

    print(
        f"funnel_screen_start cohorts={len(cohorts)} already={len(screened_keys)} "
        f"scenario={SCREEN_SCENARIO.name} notional={SCREEN_NOTIONAL}",
        flush=True,
    )

    for index, (wallet, coin, cohort_events) in enumerate(cohorts, 1):
        key = _cohort_key(wallet, coin)
        if key in screened_keys:
            continue
        summary, _ = _simulate(
            cohort_events,
            market_dir=args.market_dir,
            scenario=SCREEN_SCENARIO,
            notional=SCREEN_NOTIONAL,
            taker_fee_bps=args.taker_fee_bps,
            max_slippage_bps=args.max_slippage_bps,
            max_book_forward_ms=args.max_book_forward_ms,
        )
        row = summary | {
            "coin": coin,
            "screen_event_count": len(cohort_events),
            "checkpoint_key": key,
        }
        _append_jsonl(screen_path, row)
        screened.append(row)
        screened_keys.add(key)
        print(
            f"screen {index}/{len(cohorts)} wallet={wallet[:14]} coin={coin} "
            f"events={len(cohort_events)} actions={row['realized_actions']} "
            f"return_bps={row['net_return_bps']}",
            flush=True,
        )

    positive = [
        row
        for row in screened
        if int(row.get("realized_actions") or 0) >= max(1, args.min_screen_actions)
        and D(str(row.get("net_return_bps") or "0")) > ZERO
    ]
    positive.sort(key=_screen_rank, reverse=True)
    finalists = positive[: max(1, args.confirm_top)]

    by_key = {_cohort_key(w, c): rows for w, c, rows in cohorts}
    confirmed = _load_jsonl(confirm_path)
    confirmed_keys = {
        _confirmation_key(
            str(row["wallet_address"]),
            str(row["coin"]),
            str(row["scenario"]),
            str(row["notional_usd"]),
        )
        for row in confirmed
    }

    print(
        f"funnel_confirm_start positive={len(positive)} finalists={len(finalists)} "
        f"existing_rows={len(confirmed_keys)}",
        flush=True,
    )

    for finalist in finalists:
        wallet = str(finalist["wallet_address"]).lower()
        coin = str(finalist["coin"])
        cohort_events = by_key.get(_cohort_key(wallet, coin))
        if not cohort_events:
            continue
        for scenario in SCENARIOS:
            for notional in NOTIONALS:
                key = _confirmation_key(wallet, coin, scenario.name, str(notional))
                if key in confirmed_keys:
                    continue
                summary, slices = _simulate(
                    cohort_events,
                    market_dir=args.market_dir,
                    scenario=scenario,
                    notional=notional,
                    taker_fee_bps=args.taker_fee_bps,
                    max_slippage_bps=args.max_slippage_bps,
                    max_book_forward_ms=args.max_book_forward_ms,
                )
                row = summary | {"coin": coin, "checkpoint_key": key}
                _append_jsonl(confirm_path, row)
                for item in slices:
                    _append_jsonl(
                        slice_path,
                        item | {"wallet_address": wallet, "coin": coin},
                    )
                confirmed.append(row)
                confirmed_keys.add(key)
                print(
                    f"confirm wallet={wallet[:14]} coin={coin} scenario={scenario.name} "
                    f"notional={notional} actions={row['realized_actions']} "
                    f"return_bps={row['net_return_bps']}",
                    flush=True,
                )

    confirmed_by_cohort: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in confirmed:
        confirmed_by_cohort[_cohort_key(str(row["wallet_address"]), str(row["coin"]))].append(row)

    robust: list[dict[str, object]] = []
    for finalist in finalists:
        wallet = str(finalist["wallet_address"])
        coin = str(finalist["coin"])
        rows = confirmed_by_cohort.get(_cohort_key(wallet, coin), [])
        by_notional: dict[str, list[dict[str, object]]] = defaultdict(list)
        for row in rows:
            by_notional[str(row["notional_usd"])].append(row)
        for notional, scenario_rows in by_notional.items():
            if len({str(row["scenario"]) for row in scenario_rows}) != len(SCENARIOS):
                continue
            worst = min(D(str(row["net_return_bps"])) for row in scenario_rows)
            actions = min(int(row["realized_actions"]) for row in scenario_rows)
            if worst <= ZERO or actions < max(1, args.min_screen_actions):
                continue
            robust.append(
                {
                    "wallet_address": wallet,
                    "coin": coin,
                    "notional_usd": notional,
                    "worst_latency_return_bps": str(worst),
                    "actions_floor": actions,
                }
            )

    robust.sort(
        key=lambda row: (
            D(str(row["worst_latency_return_bps"])),
            int(row["actions_floor"]),
        ),
        reverse=True,
    )
    report = {
        "mode": "INCREMENTAL_PROFITABILITY_FUNNEL_V1",
        "real_trading": False,
        "wide_event_count": len(events),
        "screened_cohort_count": len(screened),
        "positive_screen_count": len(positive),
        "confirmed_row_count": len(confirmed),
        "robust_candidate_count": len(robust),
        "robust_candidates": robust[:100],
        "run_at": datetime.now(UTC).isoformat(),
        "boundary_counts": {
            "fetched": int(universe_payload.get("screened_wallets", 0)),
            "new_or_changed": len(universe_payload.get("registered_this_run", []))
            + len(universe_payload.get("refreshed_this_run", [])),
            "profiled": len(grouped),
            "screened": len(screened),
            "robust": len(robust),
        },
        "rejection_counts": {
            "insufficient_wallet_coin_events": len(grouped) - len(cohorts),
            "screen_non_positive_or_too_few_actions": len(screened) - len(positive),
            "confirmation_not_robust": len(finalists)
            - len({_cohort_key(str(r['wallet_address']), str(r['coin'])) for r in robust}),
        },
        "files": {
            "screening": str(screen_path),
            "confirmation": str(confirm_path),
            "realized_slices": str(slice_path),
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    queue = build_challenger_queue(
        robust,
        selection_contract_version=LANE1_SELECTION_CONTRACT_V1,
        output_path=args.output_dir / "challenger_queue.json",
        universe_state_path=args.universe_state,
        max_universe_age_hours=max(0.0, args.max_universe_age_hours),
    )
    counts = queue["counts"]
    report["boundary_counts"]["challenger"] = counts["challenger"]
    report["boundary_counts"]["prospective_shadow"] = counts["challenger"]
    tmp = report_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    tmp.replace(report_path)
    print(
        f"funnel_done screened={len(screened)} positive={len(positive)} "
        f"confirmed={len(confirmed)} robust={len(robust)} "
        f"challenger={counts['challenger']} prospective_shadow={counts['challenger']} "
        f"rejected={counts['rejected']} demoted={counts['demoted']} "
        f"timestamp={queue['generated_at']} report={report_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
