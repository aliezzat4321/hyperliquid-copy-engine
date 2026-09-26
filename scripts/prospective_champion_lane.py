#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.market.symbols import wire_coin
from hlcopy.profitability.causal_book import CausalParquetL2BookProvider
from hlcopy.profitability.lane1_funding import (
    FundingEvidenceError,
    completed_episode_funding_boundaries,
    funding_cashflows,
    resolve_funding_settlements,
    validate_completed_episode_funding_coverage,
)
from hlcopy.profitability.lane1_handoff import record_prospective_outcomes
from hlcopy.profitability.lane1_metrics import (
    LANE1_RETURN_BASIS_FUNDING_V2,
    completed_round_trip_metrics,
)
from hlcopy.profitability.portfolio_position_copy import simulate_copy_with_portfolio_capital
from hlcopy.profitability.position_copy import load_wide_events
from hlcopy.profitability.position_live_cli import NOTIONALS, SCENARIOS, _summary

D = Decimal
BASE = Path("/root/hyperliquid-audit/prospective-champions")
CFG = BASE / "config.json"
REPORT = BASE / "report.json"
DEFAULT_QUEUE = Path("/root/hyperliquid-audit/funnel/challenger_queue.json")
WIDE = Path("/mnt/HC_Volume_106576526/hyperliquid/shadow/wide-enriched-live")
MARKET = Path("/mnt/HC_Volume_106576526/hyperliquid/market-shadow")
MIN_EVALUATED_ROUND_TRIPS = 20
FUNDING_COMPLETE = "COMPLETE"
FUNDING_NOT_REQUIRED = "NOT_REQUIRED"
FUNDING_UNAVAILABLE = "UNAVAILABLE"


def atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_challenger_queue(queue_path: Path):
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    targets = []
    for row in payload.get("candidates", []):
        if row.get("status") != "challenger":
            continue
        targets.append(
            {
                "wallet": str(row["wallet_address"]).lower(),
                "coin": str(row["coin"]),
                "primary_notional": str(row["notional_usd"]),
                "prospective_start_ns": int(row["prospective_start_ns"]),
                "candidate_key": str(row["candidate_key"]),
            }
        )
    return payload, targets


def load_frozen_targets(queue_path: Path):
    return load_challenger_queue(queue_path)[1]


def _fingerprint(outcome: dict[str, object]) -> str:
    stable = {
        key: outcome.get(key)
        for key in (
            "candidate_key",
            "event_count",
            "evaluation_state",
            "actions_floor",
            "completed_round_trips_floor",
            "funding_evidence_state",
            "worst_primary_return_bps",
            "approved",
            "return_basis",
        )
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _target_events(target_spec: dict[str, object], grouped) -> tuple:
    prospective_start_ns = int(target_spec["prospective_start_ns"])
    return tuple(
        event
        for event in grouped.get(
            (str(target_spec["wallet"]).lower(), str(target_spec["coin"])), []
        )
        if event.received_at_ns >= prospective_start_ns
    )


def _funding_ranges(target_event_sets: dict[str, tuple]) -> dict[str, tuple[int, int]]:
    ranges: dict[str, tuple[int, int]] = {}
    for events in target_event_sets.values():
        if not events:
            continue
        by_coin: dict[str, list[int]] = defaultdict(list)
        for event in events:
            by_coin[event.coin].append(int(event.exchange_ts_ms))
        for coin, times in by_coin.items():
            start_ms = min(times)
            end_ms = max(times)
            existing = ranges.get(coin)
            if existing is None:
                ranges[coin] = (start_ms, end_ms)
            else:
                ranges[coin] = (min(existing[0], start_ms), max(existing[1], end_ms))
    return ranges


async def _fetch_official_funding_history(
    ranges: dict[str, tuple[int, int]],
) -> tuple[dict[str, list[dict[str, object]]], dict[str, str]]:
    """Fetch finalized Hyperliquid funding once per coin for the prospective span."""
    if not ranges:
        return {}, {}
    settings = Settings.from_env()
    rows_by_coin: dict[str, list[dict[str, object]]] = {}
    errors: dict[str, str] = {}
    async with HyperliquidHttpClient(
        settings.api_url,
        settings.leaderboard_url,
        concurrency=settings.http_concurrency,
    ) as client:
        for coin, (start_ms, end_ms) in sorted(ranges.items()):
            try:
                pages = await client.funding_history_by_time(
                    wire_coin(coin),
                    start_ms,
                    end_ms,
                )
                rows: list[dict[str, object]] = []
                for page in pages:
                    payload = page.response_payload
                    if not isinstance(payload, list):
                        continue
                    rows.extend(row for row in payload if isinstance(row, dict))
                rows_by_coin[coin] = rows
            except Exception as exc:
                # Funding evidence is an accounting dependency, not a reason to stop
                # unrelated candidates. The affected coin fails closed below.
                errors[coin] = f"{type(exc).__name__}: {exc}"
    return rows_by_coin, errors


def _required_history_rows(
    history_rows: list[dict[str, object]],
    required_boundaries: tuple[tuple[str, int], ...],
) -> list[dict[str, object]]:
    required_times = {boundary for _coin, boundary in required_boundaries}
    selected: list[dict[str, object]] = []
    for row in history_rows:
        try:
            time_ms = int(row.get("time", -1))
        except (TypeError, ValueError):
            continue
        if time_ms in required_times:
            selected.append(row)
    return selected


def _classify_primary(primary: list[dict[str, object]]) -> dict[str, object]:
    if not primary:
        return {
            "evaluation_state": "NOT_EVALUATED",
            "worst_primary_return_bps": None,
            "actions_floor": None,
            "completed_round_trips_floor": None,
            "funding_evidence_state": FUNDING_NOT_REQUIRED,
            "approved": None,
        }

    actions_floor = min(int(row["realized_actions"]) for row in primary)
    round_trips_floor = min(int(row["completed_round_trips"]) for row in primary)
    funding_states = {str(row["funding_evidence_state"]) for row in primary}
    aggregate_funding_state = (
        FUNDING_UNAVAILABLE
        if FUNDING_UNAVAILABLE in funding_states
        else FUNDING_COMPLETE
        if FUNDING_COMPLETE in funding_states
        else FUNDING_NOT_REQUIRED
    )
    base: dict[str, object] = {
        "actions_floor": actions_floor,
        "completed_round_trips_floor": round_trips_floor,
        "funding_evidence_state": aggregate_funding_state,
    }
    if aggregate_funding_state == FUNDING_UNAVAILABLE:
        return {
            **base,
            "evaluation_state": "FUNDING_EVIDENCE_UNAVAILABLE",
            "worst_primary_return_bps": None,
            "approved": None,
        }

    measured_returns = [
        D(str(row["selection_return_bps"]))
        for row in primary
        if row["selection_return_bps"] is not None
    ]
    if round_trips_floor < MIN_EVALUATED_ROUND_TRIPS or len(measured_returns) != len(primary):
        return {
            **base,
            "evaluation_state": "INSUFFICIENT_COMPLETED_ROUND_TRIPS",
            "worst_primary_return_bps": str(min(measured_returns)) if measured_returns else None,
            "approved": None,
        }

    worst = min(measured_returns)
    return {
        **base,
        "evaluation_state": "EVALUATED",
        "worst_primary_return_bps": str(worst),
        "approved": worst > 0,
    }


def main() -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").upper() == "YES":
        raise SystemExit("REAL_TRADING_ENABLED must remain NO")
    ap = argparse.ArgumentParser()
    ap.add_argument("--challenger-queue", type=Path, default=DEFAULT_QUEUE)
    args = ap.parse_args()
    BASE.mkdir(parents=True, exist_ok=True)
    if not args.challenger_queue.exists():
        raise SystemExit(f"challenger queue missing: {args.challenger_queue}")

    queue, targets = load_challenger_queue(args.challenger_queue)
    observed_ns = time.time_ns()
    observed_at = datetime.now(UTC).isoformat()
    atomic(
        CFG,
        {
            "mode": "AUTONOMOUS_FROZEN_PROSPECTIVE_V5_FUNDING_ADJUSTED",
            "updated_ns": observed_ns,
            "source_queue": str(args.challenger_queue),
            "canonical_notionals": [str(value) for value in NOTIONALS],
            "return_basis": LANE1_RETURN_BASIS_FUNDING_V2,
            "targets": targets,
            "real_trading": False,
        },
    )

    cutoff = min(
        (int(target["prospective_start_ns"]) for target in targets),
        default=observed_ns,
    )
    events = load_wide_events(WIDE, cutoff_ns=cutoff)
    grouped = defaultdict(list)
    for event in events:
        grouped[(event.wallet_address.lower(), event.coin)].append(event)

    target_event_sets = {
        str(target["candidate_key"]): _target_events(target, grouped) for target in targets
    }
    official_funding, funding_fetch_errors = asyncio.run(
        _fetch_official_funding_history(_funding_ranges(target_event_sets))
    )

    rows: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []
    for target_spec in targets:
        prospective_start_ns = int(target_spec["prospective_start_ns"])
        candidate_key = str(target_spec["candidate_key"])
        coin = str(target_spec["coin"])
        target_events = target_event_sets[candidate_key]
        target: dict[str, object] = {
            "candidate_key": candidate_key,
            "wallet_address": target_spec["wallet"],
            "coin": coin,
            "primary_notional": target_spec["primary_notional"],
            "event_count": len(target_events),
            "scenarios": [],
        }
        if target_events:
            for scenario in SCENARIOS:
                provider = CausalParquetL2BookProvider(MARKET)
                provider.prime(target_events, (scenario,))
                for notional in NOTIONALS:
                    sim = simulate_copy_with_portfolio_capital(
                        target_events,
                        provider=provider,
                        scenario=scenario,
                        notional_usd=notional,
                        taker_fee_bps=D("4.5"),
                        max_slippage_bps=D("20"),
                        max_book_forward_ms=750,
                    )
                    summary = _summary(sim)
                    trade_only_metrics = completed_round_trip_metrics(sim)
                    required_boundaries = completed_episode_funding_boundaries(sim)
                    evidence_state = FUNDING_NOT_REQUIRED
                    evidence_error: str | None = None
                    settlements = ()
                    cashflows = ()
                    funded_metrics = trade_only_metrics

                    if required_boundaries:
                        fetch_error = funding_fetch_errors.get(coin)
                        if fetch_error is not None:
                            evidence_state = FUNDING_UNAVAILABLE
                            evidence_error = fetch_error
                        else:
                            try:
                                required_rows = _required_history_rows(
                                    official_funding.get(coin, []),
                                    required_boundaries,
                                )
                                settlements = resolve_funding_settlements(
                                    MARKET,
                                    coin,
                                    required_rows,
                                )
                                validate_completed_episode_funding_coverage(sim, settlements)
                                cashflows = funding_cashflows(sim, settlements)
                                funded_metrics = completed_round_trip_metrics(
                                    sim,
                                    funding_cashflows=cashflows,
                                )
                                evidence_state = FUNDING_COMPLETE
                            except FundingEvidenceError as exc:
                                evidence_state = FUNDING_UNAVAILABLE
                                evidence_error = str(exc)

                    selection_return_bps = (
                        str(funded_metrics.return_bps)
                        if evidence_state != FUNDING_UNAVAILABLE
                        and funded_metrics.return_bps is not None
                        else None
                    )
                    target["scenarios"].append(
                        {
                            "scenario": scenario.name,
                            "notional_usd": str(notional),
                            "return_basis": LANE1_RETURN_BASIS_FUNDING_V2,
                            "realized_actions": int(summary["realized_actions"]),
                            "completed_round_trips": trade_only_metrics.completed_round_trips,
                            "selection_return_bps": selection_return_bps,
                            "funding_evidence_state": evidence_state,
                            "funding_evidence_error": evidence_error,
                            "funding_required_settlements": len(required_boundaries),
                            "funding_settlement_count": len(settlements),
                            "funding_cashflow_count": len(cashflows),
                            "funding_net_pnl_usd": (
                                str(funded_metrics.funding_net_pnl_usd)
                                if evidence_state != FUNDING_UNAVAILABLE
                                else None
                            ),
                            "completed_round_trip_net_pnl_usd": (
                                str(funded_metrics.completed_net_pnl_usd)
                                if evidence_state != FUNDING_UNAVAILABLE
                                else None
                            ),
                            "trade_only_completed_round_trip_net_pnl_usd": str(
                                trade_only_metrics.completed_net_pnl_usd
                            ),
                            "completed_round_trip_peak_gross_usd": str(
                                trade_only_metrics.completed_peak_gross_usd
                            ),
                            "closed_net_pnl_usd": str(summary["closed_net_pnl_usd"]),
                            "legacy_cumulative_return_bps": str(summary["net_return_bps"]),
                        }
                    )

        primary = [
            row
            for row in target["scenarios"]
            if row["notional_usd"] == target_spec["primary_notional"]
        ]
        classification = _classify_primary(primary)
        target.update(classification)
        rows.append(target)

        funding_errors = sorted(
            {
                str(row["funding_evidence_error"])
                for row in primary
                if row.get("funding_evidence_error")
            }
        )
        outcome: dict[str, object] = {
            "candidate_key": candidate_key,
            "observed_at": observed_at,
            "prospective_start_ns": prospective_start_ns,
            "event_count": target["event_count"],
            "evaluation_state": target["evaluation_state"],
            "actions_floor": target["actions_floor"],
            "completed_round_trips_floor": target["completed_round_trips_floor"],
            "funding_evidence_state": target["funding_evidence_state"],
            "funding_evidence_errors": funding_errors,
            "worst_primary_return_bps": target["worst_primary_return_bps"],
            "approved": target["approved"],
            "return_basis": LANE1_RETURN_BASIS_FUNDING_V2,
        }
        outcome["evidence_fingerprint"] = _fingerprint(outcome)
        outcomes.append(outcome)

    report = {
        "mode": "AUTONOMOUS_CLEAN_PROSPECTIVE_LANE_V5_FUNDING_ADJUSTED",
        "cutoff_ns": cutoff,
        "age_hours": (time.time_ns() - cutoff) / 3.6e12,
        "real_trading": False,
        "challenger_queue_generated_at": queue.get("generated_at"),
        "challenger_queue_counts": queue.get("counts", {}),
        "canonical_notionals": [str(value) for value in NOTIONALS],
        "return_basis": LANE1_RETURN_BASIS_FUNDING_V2,
        "selection_evidence_unit": "COMPLETED_FOLLOWER_ROUND_TRIP_AFTER_FUNDING",
        "min_evaluated_round_trips": MIN_EVALUATED_ROUND_TRIPS,
        "funding_fetch_errors": funding_fetch_errors,
        "targets": rows,
        "challenger_count": len(targets),
        "prospective_shadow_count": sum(1 for row in rows if row["event_count"] > 0),
        "evaluated_count": sum(1 for row in rows if row["evaluation_state"] == "EVALUATED"),
        "funding_evidence_unavailable_count": sum(
            1 for row in rows if row["evaluation_state"] == "FUNDING_EVIDENCE_UNAVAILABLE"
        ),
        "insufficient_evidence_count": sum(
            1 for row in rows if row["evaluation_state"] != "EVALUATED"
        ),
        "approved_count": sum(1 for row in rows if row["approved"] is True),
        "rejections": queue.get("rejections", [])
        or (
            []
            if targets
            else [{"reason": "NO_ACTIVE_CHALLENGERS", "timestamp_ns": time.time_ns()}]
        ),
        "demoted": queue.get("demoted", []),
    }
    atomic(REPORT, report)
    record_prospective_outcomes(args.challenger_queue, outcomes)

    print("=== PROSPECTIVE CHAMPIONS ===")
    print(
        "age_hours=",
        round(report["age_hours"], 3),
        "evaluated=",
        report["evaluated_count"],
        "approved=",
        report["approved_count"],
        "funding_unavailable=",
        report["funding_evidence_unavailable_count"],
    )
    for row in rows:
        print(
            row["wallet_address"][:14],
            row["coin"],
            "events=",
            row["event_count"],
            "state=",
            row["evaluation_state"],
            "funding=",
            row["funding_evidence_state"],
            "round_trips_floor=",
            row["completed_round_trips_floor"],
            "worst_primary_bps=",
            row["worst_primary_return_bps"],
            "APPROVED=",
            row["approved"],
        )
    print("report=", REPORT)


if __name__ == "__main__":
    main()
