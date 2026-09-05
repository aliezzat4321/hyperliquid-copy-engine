from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from . import CONTRACT_ID
from .book import Lane3CausalBookProvider
from .costs import CostCompleteness, measure_leg, position_economics, scenario_net
from .funding import attribute_funding, load_cached_funding
from .ledger import load_audit_jsonl, reconstruct_ledger
from .promotion import evaluate_promotion
from .reconstruction import Disposition, ReconstructedPosition
from .report import DiagnosticSlice, Lane3Report, PromotableSlice
from .statistics import day_block_bootstrap

D = Decimal


def add_arguments(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="lane3_command", required=True)
    for name in ("coverage-probe", "report", "freeze"):
        command = commands.add_parser(name)
        command.add_argument("--audit", required=True, type=Path)
        command.add_argument("--state", required=True, type=Path)
        command.add_argument("--config", type=Path, default=Path("config/lane3_measurement.json"))
        command.add_argument("--output", required=True, type=Path)
    commands.choices["freeze"].add_argument("--report", required=True, type=Path)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _positions(args: argparse.Namespace):
    return reconstruct_ledger(load_audit_jsonl(args.audit), _load(args.state))


def coverage(args: argparse.Namespace) -> dict[str, Any]:
    cfg = _load(args.config)
    ledger = _positions(args)
    provider = Lane3CausalBookProvider(
        Path(cfg["tape_root"]), max_age_ms=cfg["max_book_age_ms"]
    )
    decisions = [
        (position.coin, leg.timestamp_ms)
        for position in ledger.positions
        for leg in position.entry_legs
        + ([position.exit_leg] if position.exit_leg else [])
    ]
    measured = 0
    arrival_offset_ms = (
        float(cfg["follower_submit_latency_ms"])
        + float(cfg["transport_latency_ms"])
    )
    for coin, timestamp in decisions:
        arrival_ms = timestamp + arrival_offset_ms
        book = provider.at_or_before(coin, arrival_ms)
        if book is not None and book.received_at_ns <= arrival_ms * 1_000_000:
            measured += 1
    return {"decision_legs": len(decisions), "covered_legs": measured,
            "coverage_share": measured / len(decisions) if decisions else 0.0,
            "max_book_age_ms": cfg["max_book_age_ms"],
            "arrival_latency_ms": arrival_offset_ms,
            "evidence_basis": "CAUSAL_SIMULATED_ORDER_ARRIVAL"}


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _slice(
    slice_id: str, positions: list[ReconstructedPosition], cfg: dict[str, Any],
    provider: Lane3CausalBookProvider, policy: dict[str, Any], report_ms: int,
) -> PromotableSlice:
    closed = [p for p in positions if p.disposition == Disposition.VALID_CLOSED and p.exit_leg]
    opened = [p for p in positions if p.disposition == Disposition.OPEN]
    quarantined = [
        p
        for p in positions
        if p.disposition not in {Disposition.OPEN, Disposition.VALID_CLOSED}
    ]
    rates = load_cached_funding(Path(cfg["funding_cache_path"]), positions)
    economics = []
    latencies = [x for position in positions for x in position.detection_latencies_ms]
    chases = [x for position in positions for x in position.chase_bps]
    all_costs = []
    for position in closed:
        legs = position.entry_legs + [position.exit_leg]
        costs = [measure_leg(position, leg, provider, taker_rate=D(str(cfg["taker_rate"])),
                             max_slippage_bps=D(str(cfg["max_slippage_bps"])),
                             follower_submit_latency_ms=float(
                                 cfg["follower_submit_latency_ms"]
                             ),
                             transport_latency_ms=float(cfg["transport_latency_ms"]))
                 for leg in legs]
        funding = attribute_funding(position, rates, end_ms=position.exit_leg.timestamp_ms)
        economics.append(position_economics(position, costs, funding_usd=funding.funding_usd,
                                            funding_measured=funding.measured))
        all_costs.extend(costs)
    measured = bool(economics) and all(
        e.cost_completeness == CostCompleteness.MEASURED for e in economics
    )
    nets = [float(e.net_pnl_usd) for e in economics if e.net_pnl_usd is not None]
    returns = [float(e.net_return_bps) for e in economics if e.net_return_bps is not None]
    observations = [
        (
            datetime.fromtimestamp(p.exit_leg.timestamp_ms / 1000, UTC).date().isoformat(),
            value,
        )
        for p, value in zip(closed, returns, strict=False)
    ]
    boot = day_block_bootstrap(observations, seed=int(cfg["bootstrap_seed"]),
                               confidence_level=float(policy["thresholds"]["confidence_level"]))
    gross = sum((e.gross_mid_to_mid_pnl_usd for e in economics), D("0"))
    fees = sum((e.entry_fees_usd + e.exit_fee_usd for e in economics), D("0"))
    entry_notional = sum((p.entry_notional for p in closed), D("0"))
    net_total = (
        sum((e.net_pnl_usd for e in economics if e.net_pnl_usd is not None), D("0"))
        if measured
        else None
    )
    days = sorted(
        {
            datetime.fromtimestamp(
                p.entry_legs[0].timestamp_ms / 1000, UTC
            ).date().isoformat()
            for p in positions
        }
    )
    scenarios = []
    if not measured:
        for bps in cfg["scenario_round_trip_bps"]:
            values = [scenario_net(p, e.entry_fees_usd + e.exit_fee_usd, e.funding_usd, D(str(bps)))
                      for p, e in zip(closed, economics, strict=False)]
            scenarios.append({"label": "execution_cost_scenario_shadow_edge", "round_trip_bps": bps,
                              "pnl_usd": float(sum((v for v in values if v is not None), D("0")))
                              if values and all(v is not None for v in values) else None})
    positive = [value for value in nets if value > 0]
    negative = [value for value in nets if value < 0]
    concentration = max(positive) / sum(positive) if positive else None
    cost_status = "MEASURED" if measured else "SCENARIO_ONLY"
    return PromotableSlice(
        slice_id, None if slice_id == "aggregate" else positions[0].trader,
        None if slice_id == "aggregate" else positions[0].coin, len(closed), len(opened),
        len(quarantined), len(days), len({day for day, _ in observations}),
        (
            datetime.fromtimestamp(
                min(p.entry_legs[0].timestamp_ms for p in positions) / 1000, UTC
            ).isoformat()
            if positions
            else None
        ),
        (
            datetime.fromtimestamp(
                max(p.entry_legs[0].timestamp_ms for p in positions) / 1000, UTC
            ).isoformat()
            if positions
            else None
        ),
        "RETROSPECTIVE_WHOLE_LEDGER", float(gross), float(fees),
        {
            "half_spread": float(
                sum((c.half_spread_usd or D("0") for c in all_costs), D("0"))
            )
            if measured
            else None,
            "impact": float(
                sum((c.impact_usd or D("0") for c in all_costs), D("0"))
            )
            if measured
            else None,
        },
        (
            float(sum((e.funding_usd or D("0") for e in economics), D("0")))
            if economics and all(e.funding_usd is not None for e in economics)
            else None
        ),
        float(net_total) if net_total is not None else None,
        sum(returns) / len(returns) if measured and returns else None,
        (
            float(net_total / entry_notional * D("10000"))
            if net_total is not None and entry_notional
            else None
        ),
        len(positive) / len(nets) if nets else None,
        sum(positive) / abs(sum(negative)) if negative else None,
        _max_drawdown(nets) if nets else None, concentration,
        _breakeven_trade_weighted(closed, economics),
        (
            float(
                (gross - fees + sum((e.funding_usd or D("0") for e in economics), D("0")))
                / sum((p.entry_notional + p.exit_leg.notional for p in closed), D("0"))
                * D("20000")
            )
            if closed
            else None
        ),
        {"count": len(opened),
         "notional_usd": float(sum((p.entry_notional for p in opened), D("0"))),
         "closed_notional_usd": float(entry_notional), "mtm_usd": "UNAVAILABLE",
         "max_age_hours": max(
             ((report_ms - p.entry_legs[0].timestamp_ms) / 3_600_000 for p in opened),
             default=0,
         ),
         "adverse_excursion": "UNAVAILABLE"},
        {"detection_p50": median(latencies) if latencies else None,
         "p90": _percentile(latencies, .9),
         "max": max(latencies) if latencies else None},
        {"p50": median(chases) if chases else None, "p90": _percentile(chases, .9)},
        {"legs_complete_share": (
             sum(c.completeness == CostCompleteness.MEASURED for c in all_costs)
             / len(all_costs) if all_costs else 0),
         "execution_evidence_basis": "CAUSAL_SIMULATED_ORDER_ARRIVAL",
         "follower_submit_latency_ms": cfg["follower_submit_latency_ms"],
         "transport_latency_ms": cfg["transport_latency_ms"],
         "worst_leg_slippage_bps": max(
             (float(c.crossing_bps) for c in all_costs if c.crossing_bps is not None),
             default=None)},
        cost_status, asdict(boot), boot.p_value if measured else None, None,
        policy["policy_version"], "NOT_PROMOTABLE", scenarios)


def _max_drawdown(values: list[float]) -> float:
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _breakeven_trade_weighted(positions, economics) -> float | None:
    values = []
    for position, econ in zip(positions, economics, strict=False):
        if position.exit_leg and econ.funding_usd is not None:
            executed = position.entry_notional + position.exit_leg.notional
            numerator = (
                econ.gross_mid_to_mid_pnl_usd
                - econ.entry_fees_usd
                - econ.exit_fee_usd
                + econ.funding_usd
            )
            values.append(float(numerator / executed * D("20000")))
    return sum(values) / len(values) if values else None


def build_report(args: argparse.Namespace) -> Lane3Report:
    cfg = _load(args.config)
    policy = _load(Path(cfg["promotion_policy_path"]))
    ledger = _positions(args)
    provider = Lane3CausalBookProvider(
        Path(cfg["tape_root"]), max_age_ms=cfg["max_book_age_ms"]
    )
    now = datetime.now(UTC)
    groups: dict[tuple[str, str], list[ReconstructedPosition]] = {}
    for position in ledger.positions:
        groups.setdefault((position.trader, position.coin), []).append(position)
    report_ms = int(now.timestamp() * 1000)
    slices = [_slice("aggregate", ledger.positions, cfg, provider, policy, report_ms)]
    slices.extend(
        _slice(f"{trader}:{coin}", values, cfg, provider, policy, report_ms)
        for (trader, coin), values in sorted(groups.items())
    )
    true_orphans = sum(cause == "TRUE_ORPHAN" for cause in ledger.orphan_causes.values())
    share = true_orphans / ledger.orphan_closes if ledger.orphan_closes else 0.0
    for item in slices:
        verdict = evaluate_promotion(item, policy_path=Path(cfg["promotion_policy_path"]),
                                     reconciliation_ok=True, prospective=False,
                                     true_orphan_share=share, signs_agree=True)
        object.__setattr__(item, "verdict", verdict.verdict)
    gross = sum(item.gross_mid_to_mid_pnl_usd for item in slices[:1])
    return Lane3Report(now.isoformat(), CONTRACT_ID, policy["policy_version"], ledger.reconcile(),
                       coverage(args), slices,
                       [DiagnosticSlice("signal_lag_decay", {"add_count": "diagnostic_only"},
                                        {"non_additive": True}),
                        DiagnosticSlice(
                            "orphan_classification", {},
                            {k: v.value for k, v in ledger.orphan_causes.items()},
                        )],
                       {"value": gross, "is_headline": False,
                        "warning": "gross; excludes fees, spread, impact, funding"})


def freeze(args: argparse.Namespace) -> dict[str, Any]:
    report = _load(args.report)
    now = datetime.now(UTC).isoformat()
    cells = [item for item in report["slices"] if item["slice_id"] != "aggregate"]
    cells.sort(
        key=lambda item: item.get("net_pnl_usd")
        if item.get("net_pnl_usd") is not None
        else float("-inf"),
        reverse=True,
    )
    contract_path = Path("docs/INVO_NOTIFICATION_NET_EDGE.md")
    contract_sha = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    return {"contract_id": CONTRACT_ID, "contract_sha256": contract_sha,
            "policy_version": report["policy_version"], "frozen_at": now, "T_freeze": now,
            "universe": {
                "trader_coin_cells": [item["slice_id"] for item in cells], "S": len(cells)
            },
            "candidates": [
                {"slice_id": item["slice_id"], "assessmentEligibleAtMs": None}
                for item in cells[:5]
            ],
            "statistic": "net_return_bps_trade_weighted", "cluster_unit": "UTC_DAY",
            "bootstrap_seed": 193, "fwer_alpha": 0.10, "K": min(5, len(cells))}


def run(args: argparse.Namespace) -> None:
    if args.lane3_command == "coverage-probe":
        result = coverage(args)
    elif args.lane3_command == "report":
        result = build_report(args).as_dict()
    else:
        result = freeze(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
