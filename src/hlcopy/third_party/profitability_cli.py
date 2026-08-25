from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from hlcopy.copyability.backtest import BacktestConfig, run_backtest
from hlcopy.profitability.causal_book import CausalParquetL2BookProvider
from hlcopy.profitability.portfolio_position_copy import simulate_copy_with_portfolio_capital
from hlcopy.profitability.position_copy import CopyFillEvent, load_wide_events
from hlcopy.profitability.position_live_cli import NOTIONALS, SCENARIOS, _summary
from hlcopy.shadow.registry import WalletRegistry, WalletSpec
from hlcopy.signals.invo import load_invo_closed_trades

D = Decimal
ZERO = D("0")
BASE_SCENARIO = SCENARIOS[2]  # LIVE_500MS
BASE_NOTIONAL = D("5000")
MIN_ROBUST_ACTIONS = 3
MIN_ROBUST_EXECUTION_PCT = 60.0
VALIDATION_ACTIONS = 10
VALIDATION_EXECUTION_PCT = 80.0


def _metadata(wallet: WalletSpec) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for segment in wallet.notes.split(";"):
        key, separator, value = segment.strip().partition("=")
        if separator and key and value:
            parsed[key.strip()] = value.strip()
    return {
        "source": parsed.get("third_party_source", "unknown"),
        "identity": parsed.get("third_party_identity", wallet.id),
        "evidence_sha256": parsed.get("evidence_sha256", ""),
        "resolver_rule_version": parsed.get("resolver_rule_version", ""),
    }


def _decimal(row: dict[str, object], key: str) -> Decimal:
    try:
        return D(str(row.get(key) or "0"))
    except ArithmeticError:
        return ZERO


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if fields:
            writer.writeheader()
            writer.writerows(rows)


def _simulate(
    events: tuple[CopyFillEvent, ...],
    *,
    provider: CausalParquetL2BookProvider,
    scenario,
    notional: Decimal,
    taker_fee_bps: Decimal,
    max_slippage_bps: Decimal,
    max_book_forward_ms: int,
):
    simulation = simulate_copy_with_portfolio_capital(
        events,
        provider=provider,
        scenario=scenario,
        notional_usd=notional,
        taker_fee_bps=max(ZERO, taker_fee_bps),
        max_slippage_bps=max(D("0.1"), max_slippage_bps),
        max_book_forward_ms=max(1, max_book_forward_ms),
    )
    return _summary(simulation), list(simulation.realized_slices)


def _direction_breakdown(slices) -> list[dict[str, object]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for item in slices:
        grouped[item.direction].append(item)
    output: list[dict[str, object]] = []
    for direction, rows in sorted(grouped.items()):
        net = sum((item.net_pnl_usd for item in rows), ZERO)
        wins = sum(item.net_pnl_usd > ZERO for item in rows)
        output.append(
            {
                "direction": direction,
                "realized_actions": len(rows),
                "net_pnl_usd": str(net),
                "win_pct": str(D(wins) / D(len(rows)) * D("100")) if rows else None,
            }
        )
    return output


def _robust_notionals(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    by_notional: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_notional[str(row["notional_usd"])].append(row)
    robust: list[dict[str, object]] = []
    for notional, scenario_rows in by_notional.items():
        if len({str(row["scenario"]) for row in scenario_rows}) != len(SCENARIOS):
            continue
        worst_return = min(_decimal(row, "net_return_bps") for row in scenario_rows)
        actions_floor = min(int(row.get("realized_actions") or 0) for row in scenario_rows)
        execution_floor = min(float(row.get("execution_pct") or 0.0) for row in scenario_rows)
        if (
            worst_return <= ZERO
            or actions_floor < MIN_ROBUST_ACTIONS
            or execution_floor < MIN_ROBUST_EXECUTION_PCT
        ):
            continue
        robust.append(
            {
                "notional_usd": notional,
                "worst_latency_return_bps": str(worst_return),
                "actions_floor": actions_floor,
                "execution_floor_pct": execution_floor,
            }
        )
    robust.sort(
        key=lambda row: (
            D(str(row["worst_latency_return_bps"])),
            int(row["actions_floor"]),
        ),
        reverse=True,
    )
    return robust


def _copyability_score(
    *,
    base_rows: list[dict[str, object]],
    robust_notionals: list[dict[str, object]],
) -> tuple[float, dict[str, float]]:
    if not base_rows:
        return 0.0, {
            "execution": 0.0,
            "latency_robustness": 0.0,
            "evidence_depth": 0.0,
            "scale_robustness": 0.0,
        }
    execution = min(float(row.get("execution_pct") or 0.0) for row in base_rows)
    positive = sum(_decimal(row, "net_return_bps") > ZERO for row in base_rows)
    latency = positive / len(SCENARIOS) * 100.0
    actions = min(int(row.get("realized_actions") or 0) for row in base_rows)
    evidence = min(100.0, actions / 30.0 * 100.0)
    scale = min(100.0, len(robust_notionals) / len(NOTIONALS) * 100.0)
    components = {
        "execution": execution,
        "latency_robustness": latency,
        "evidence_depth": evidence,
        "scale_robustness": scale,
    }
    score = execution * 0.40 + latency * 0.25 + evidence * 0.20 + scale * 0.15
    return round(score, 2), components


def _status(
    *,
    event_count: int,
    base_500ms: dict[str, object] | None,
    base_rows: list[dict[str, object]],
    robust: list[dict[str, object]],
) -> str:
    if event_count == 0 or base_500ms is None:
        return "COLLECTING"
    actions_floor = min(int(row.get("realized_actions") or 0) for row in base_rows)
    if actions_floor < MIN_ROBUST_ACTIONS:
        return "EARLY"
    if _decimal(base_500ms, "net_return_bps") <= ZERO:
        return "NEGATIVE_AT_BASE"
    if not robust:
        return "PROMISING_NEEDS_ROBUSTNESS"
    robust_actions = min(int(row["actions_floor"]) for row in robust)
    if robust_actions < VALIDATION_ACTIONS:
        return "ROBUST_EARLY"
    if robust_actions < 30:
        return "ROBUST_DEVELOPING"
    return "ROBUST_STRONG"


def _load_invo_queue(path: Path) -> dict[str, Path]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = payload.get("queue", []) if isinstance(payload, dict) else []
    root = path.parent.parent.resolve()
    result: dict[str, Path] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = str(row.get("portfolio_id") or "").strip()
        raw_csv = str(row.get("resolver_csv") or "").strip()
        if not identity or not raw_csv:
            continue
        candidate = Path(raw_csv).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            result[identity] = candidate
    return result


def _source_history_prescreen(
    wallet: WalletSpec,
    *,
    invo_queue: dict[str, Path],
    taker_fee_bps: Decimal,
) -> dict[str, object] | None:
    meta = _metadata(wallet)
    if meta["source"] != "invo":
        return None
    evidence = invo_queue.get(meta["identity"])
    if evidence is None:
        return {
            "mode": "SOURCE_PRICE_ONLY_PRE_SCREEN",
            "available": False,
            "reason": "INVO_RESOLVER_EVIDENCE_NOT_FOUND",
        }
    imported = load_invo_closed_trades(evidence)
    matrix: list[dict[str, object]] = []
    for leverage in (D("1"), D("3"), D("5")):
        summary, _rows = run_backtest(
            imported.signals,
            BacktestConfig(
                starting_capital=D("10000"),
                latency_ms=0,
                follower_leverage=leverage,
                taker_fee_rate=max(ZERO, taker_fee_bps) / D("10000"),
                max_slippage_bps=D("20"),
                max_margin_fraction_per_trade=D("0.20"),
                max_total_margin_fraction=D("0.80"),
            ),
            book_provider=None,
        )
        matrix.append(summary.to_dict())
    return {
        "mode": "SOURCE_PRICE_ONLY_PRE_SCREEN",
        "available": True,
        "selection_bias": "KNOWN_WINNER_SOURCE_SELECTION",
        "execution_truth": False,
        "latency_price_impact": "NOT_MODELED",
        "slippage": "NOT_MODELED",
        "purpose": "historical directionality only; never sufficient for promotion",
        "input_trades": len(imported.signals),
        "rejected_rows": len(imported.rejected_rows),
        "matrix": matrix,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.third_party.profitability_cli")
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--wide-enriched-dir", required=True, type=Path)
    parser.add_argument("--cutoff-ns-file", required=True, type=Path)
    parser.add_argument("--market-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--invo-resolution-queue", type=Path)
    parser.add_argument("--taker-fee-bps", type=Decimal, default=D("4.5"))
    parser.add_argument("--max-slippage-bps", type=Decimal, default=D("20"))
    parser.add_argument("--max-book-forward-ms", type=int, default=750)
    return parser


def main() -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("third-party profitability refuses REAL_TRADING_ENABLED=YES")
    args = build_parser().parse_args()
    registry = WalletRegistry(args.registry)
    wallets = tuple(
        wallet
        for wallet in registry.load()
        if wallet.enabled and wallet.source_type == "hyperliquid_wallet"
    )
    cutoff_ns = int(args.cutoff_ns_file.read_text(encoding="utf-8").strip())
    all_events = load_wide_events(args.wide_enriched_dir, cutoff_ns=cutoff_ns)
    addresses = {wallet.source_ref.lower() for wallet in wallets}
    events = tuple(event for event in all_events if event.wallet_address.lower() in addresses)
    by_wallet: dict[str, list[CopyFillEvent]] = defaultdict(list)
    for event in events:
        by_wallet[event.wallet_address.lower()].append(event)

    provider = CausalParquetL2BookProvider(args.market_dir) if events else None
    if provider is not None:
        provider.prime(events, SCENARIOS)

    invo_queue = (
        _load_invo_queue(args.invo_resolution_queue)
        if args.invo_resolution_queue is not None
        else {}
    )
    scenario_rows: list[dict[str, object]] = []
    coin_rows: list[dict[str, object]] = []
    scorecards: list[dict[str, object]] = []

    for wallet in wallets:
        address = wallet.source_ref.lower()
        wallet_events = tuple(by_wallet.get(address, []))
        meta = _metadata(wallet)
        rows: list[dict[str, object]] = []
        base_slices = []
        if provider is not None and wallet_events:
            for scenario in SCENARIOS:
                for notional in NOTIONALS:
                    summary, slices = _simulate(
                        wallet_events,
                        provider=provider,
                        scenario=scenario,
                        notional=notional,
                        taker_fee_bps=args.taker_fee_bps,
                        max_slippage_bps=args.max_slippage_bps,
                        max_book_forward_ms=args.max_book_forward_ms,
                    )
                    row = summary | {
                        "third_party_source": meta["source"],
                        "third_party_identity": meta["identity"],
                        "wallet_label": wallet.label,
                    }
                    rows.append(row)
                    scenario_rows.append(row)
                    if scenario.name == BASE_SCENARIO.name and notional == BASE_NOTIONAL:
                        base_slices = slices

            by_coin: dict[str, list[CopyFillEvent]] = defaultdict(list)
            for event in wallet_events:
                by_coin[event.coin].append(event)
            for coin, coin_events in sorted(by_coin.items()):
                summary, _ = _simulate(
                    tuple(coin_events),
                    provider=provider,
                    scenario=BASE_SCENARIO,
                    notional=BASE_NOTIONAL,
                    taker_fee_bps=args.taker_fee_bps,
                    max_slippage_bps=args.max_slippage_bps,
                    max_book_forward_ms=args.max_book_forward_ms,
                )
                coin_rows.append(
                    summary
                    | {
                        "coin": coin,
                        "third_party_source": meta["source"],
                        "third_party_identity": meta["identity"],
                        "wallet_label": wallet.label,
                    }
                )

        base_rows = [row for row in rows if str(row["notional_usd"]) == str(BASE_NOTIONAL)]
        base_500ms = next(
            (row for row in base_rows if str(row["scenario"]) == BASE_SCENARIO.name),
            None,
        )
        robust = _robust_notionals(rows)
        score, components = _copyability_score(
            base_rows=base_rows,
            robust_notionals=robust,
        )
        status = _status(
            event_count=len(wallet_events),
            base_500ms=base_500ms,
            base_rows=base_rows,
            robust=robust,
        )
        robust_actions = min((int(row["actions_floor"]) for row in robust), default=0)
        robust_execution = min(
            (float(row["execution_floor_pct"]) for row in robust),
            default=0.0,
        )
        scorecards.append(
            {
                "source": meta["source"],
                "identity": meta["identity"],
                "label": wallet.label,
                "wallet": address,
                "identity_evidence_sha256": meta["evidence_sha256"],
                "resolver_rule_version": meta["resolver_rule_version"],
                "prospective_events": len(wallet_events),
                "status": status,
                "copyability_score": score,
                "score_components": components,
                "base_scenario": base_500ms,
                "robust_notionals": robust,
                "direction_breakdown_base": _direction_breakdown(base_slices),
                "validation_candidate": (
                    bool(robust)
                    and robust_actions >= VALIDATION_ACTIONS
                    and robust_execution >= VALIDATION_EXECUTION_PCT
                ),
                "historical_pre_screen": _source_history_prescreen(
                    wallet,
                    invo_queue=invo_queue,
                    taker_fee_bps=args.taker_fee_bps,
                ),
            }
        )

    scorecards.sort(
        key=lambda row: (
            bool(row["validation_candidate"]),
            float(row["copyability_score"]),
            int(row["prospective_events"]),
        ),
        reverse=True,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": "THIRD_PARTY_COPYABILITY_PROFITABILITY_V1",
        "generated_at": datetime.now(UTC).isoformat(),
        "real_trading": False,
        "prospective_cutoff_ns": cutoff_ns,
        "prospective_event_count": len(events),
        "wallet_count": len(wallets),
        "score_formula": {
            "execution_pct_floor_weight": 0.40,
            "positive_latency_scenarios_weight": 0.25,
            "evidence_depth_to_30_actions_weight": 0.20,
            "robust_notional_count_weight": 0.15,
        },
        "execution_model": {
            "latency_scenarios": [scenario.name for scenario in SCENARIOS],
            "notionals_usd": [str(value) for value in NOTIONALS],
            "taker_fee_bps": str(args.taker_fee_bps),
            "max_slippage_bps": str(args.max_slippage_bps),
            "max_book_forward_ms": args.max_book_forward_ms,
            "capital_model": "CAUSAL_PORTFOLIO_POSITION_COPY",
        },
        "scorecards": scorecards,
        "safety": {
            "research_only": True,
            "auto_validation": False,
            "auto_live_approval": False,
            "historical_pre_screen_can_promote": False,
            "funding": "NOT_MODELED_YET",
            "continuous_mtm": "NOT_MODELED_YET",
            "maintenance_margin": "NOT_MODELED_YET",
            "liquidation_survival": "NOT_MODELED_YET",
        },
    }
    report_path = args.output_dir / "third_party_scorecard.json"
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(report_path)
    _write_csv(args.output_dir / "third_party_scenarios.csv", scenario_rows)
    _write_csv(args.output_dir / "third_party_coin_screen.csv", coin_rows)

    print(
        f"third_party_profitability wallets={len(wallets)} events={len(events)} "
        f"scorecards={len(scorecards)}"
    )
    for row in scorecards:
        base = row.get("base_scenario") or {}
        print(
            f"source={row['source']} identity={row['identity']} wallet={str(row['wallet'])[:14]} "
            f"status={row['status']} score={row['copyability_score']} "
            f"events={row['prospective_events']} actions={base.get('realized_actions', 0)} "
            f"net_bps={base.get('net_return_bps', 'NA')} "
            f"validation_candidate={row['validation_candidate']}"
        )
    print(f"wrote {report_path}")


if __name__ == "__main__":
    main()
