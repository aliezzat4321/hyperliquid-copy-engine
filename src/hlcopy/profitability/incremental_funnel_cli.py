from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hlcopy.profitability.causal_book import CausalParquetL2BookProvider
from hlcopy.profitability.lane1_funding import (
    FundingEvidenceError,
    completed_episode_funding_boundaries,
    funding_cashflows,
    resolve_funding_settlements,
    validate_completed_episode_funding_coverage,
)
from hlcopy.profitability.lane1_funding_source import (
    fetch_official_funding_history,
    funding_ranges_for_event_groups,
    required_history_rows,
)
from hlcopy.profitability.lane1_handoff import (
    LANE1_SELECTION_CONTRACT_V1,
    build_challenger_queue,
)
from hlcopy.profitability.lane1_metrics import (
    LANE1_RETURN_BASIS_FUNDING_V2,
    completed_round_trip_metrics,
)
from hlcopy.profitability.portfolio_position_copy import simulate_copy_with_portfolio_capital
from hlcopy.profitability.position_copy import CopyFillEvent, load_wide_events
from hlcopy.profitability.position_live_cli import NOTIONALS, SCENARIOS, _summary

D = Decimal
ZERO = D("0")
BPS = D("10000")
SCREEN_SCENARIO = SCENARIOS[2]  # LIVE_500MS
SCREEN_NOTIONAL = D("5000")
DEFAULT_UNIVERSE_STATE = Path(
    "/mnt/HC_Volume_106576526/hyperliquid/discovery/universe_state.json"
)
DEFAULT_PROSPECTIVE_REPORT = Path("/root/hyperliquid-audit/prospective-champions/report.json")
RETURN_BASIS = LANE1_RETURN_BASIS_FUNDING_V2
FUNDING_COMPLETE = "COMPLETE"
FUNDING_NOT_REQUIRED = "NOT_REQUIRED"
FUNDING_UNAVAILABLE = "UNAVAILABLE"


def _write_jsonl_atomic(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _cohort_key(wallet: str, coin: str) -> str:
    return f"{wallet.lower()}|{coin}"


def _screen_key(wallet: str, coin: str, window_id: str) -> str:
    return f"{_cohort_key(wallet, coin)}|{window_id}"


def _confirmation_key(
    wallet: str,
    coin: str,
    scenario: str,
    notional: str,
    window_id: str,
) -> str:
    return f"{_cohort_key(wallet, coin)}|{scenario}|{notional}|{window_id}"


def _window_id(
    wallet: str,
    coin: str,
    events: tuple[CopyFillEvent, ...],
    split_index: int,
    cutoff_ns: int,
) -> str:
    first_ns = events[0].received_at_ns
    last_ns = events[-1].received_at_ns
    payload = (
        f"{_cohort_key(wallet, coin)}|{cutoff_ns}|{first_ns}|{last_ns}|"
        f"{len(events)}|{split_index}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _split_oos(
    events: tuple[CopyFillEvent, ...],
    *,
    min_screen_events: int,
    min_confirm_events: int,
) -> tuple[tuple[CopyFillEvent, ...], tuple[CopyFillEvent, ...]] | None:
    ordered = tuple(
        sorted(
            events,
            key=lambda row: (row.received_at_ns, row.exchange_ts_ms, row.tid),
        )
    )
    if len(ordered) < min_screen_events + min_confirm_events:
        return None
    split_index = max(min_screen_events, int(len(ordered) * 0.60))
    split_index = min(split_index, len(ordered) - min_confirm_events)
    if split_index <= 0 or split_index >= len(ordered):
        return None
    screen = ordered[:split_index]
    confirm = ordered[split_index:]
    if screen[-1].received_at_ns >= confirm[0].received_at_ns:
        boundary_ns = confirm[0].received_at_ns
        split_index = next(
            (
                index
                for index, event in enumerate(ordered)
                if event.received_at_ns >= boundary_ns
            ),
            split_index,
        )
        if split_index < min_screen_events or len(ordered) - split_index < min_confirm_events:
            return None
        screen = ordered[:split_index]
        confirm = ordered[split_index:]
        if screen[-1].received_at_ns >= confirm[0].received_at_ns:
            return None
    return screen, confirm


def _selection_return_bps(summary: dict[str, object], notional: Decimal) -> Decimal | None:
    """Legacy normalization retained only for compatibility tests and diagnostics."""
    actions = int(summary.get("realized_actions") or 0)
    if actions <= 0 or notional <= ZERO:
        return None
    net_pnl = D(str(summary.get("closed_net_pnl_usd") or "0"))
    return net_pnl / (notional * D(actions)) * BPS


def _simulate(
    events: tuple[CopyFillEvent, ...],
    *,
    market_dir: Path,
    scenario,
    notional: Decimal,
    taker_fee_bps: Decimal,
    max_slippage_bps: Decimal,
    max_book_forward_ms: int,
    funding_history_rows: list[dict[str, object]],
    funding_fetch_error: str | None,
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
    trade_only_metrics = completed_round_trip_metrics(sim)
    required_boundaries = completed_episode_funding_boundaries(sim)
    funding_state = FUNDING_NOT_REQUIRED
    funding_error: str | None = None
    settlements = ()
    cashflows = ()
    metrics = trade_only_metrics

    if required_boundaries:
        if funding_fetch_error is not None:
            funding_state = FUNDING_UNAVAILABLE
            funding_error = funding_fetch_error
        else:
            try:
                rows = required_history_rows(funding_history_rows, required_boundaries)
                coin = events[0].coin if events else ""
                settlements = resolve_funding_settlements(market_dir, coin, rows)
                validate_completed_episode_funding_coverage(sim, settlements)
                cashflows = funding_cashflows(sim, settlements)
                metrics = completed_round_trip_metrics(sim, funding_cashflows=cashflows)
                funding_state = FUNDING_COMPLETE
            except FundingEvidenceError as exc:
                funding_state = FUNDING_UNAVAILABLE
                funding_error = str(exc)

    summary["selection_return_bps"] = (
        str(metrics.return_bps)
        if funding_state != FUNDING_UNAVAILABLE and metrics.return_bps is not None
        else None
    )
    summary["completed_round_trips"] = trade_only_metrics.completed_round_trips
    summary["completed_round_trip_net_pnl_usd"] = (
        str(metrics.completed_net_pnl_usd) if funding_state != FUNDING_UNAVAILABLE else None
    )
    summary["trade_only_completed_round_trip_net_pnl_usd"] = str(
        trade_only_metrics.completed_net_pnl_usd
    )
    summary["completed_round_trip_peak_gross_usd"] = str(
        trade_only_metrics.completed_peak_gross_usd
    )
    summary["funding_net_pnl_usd"] = (
        str(metrics.funding_net_pnl_usd) if funding_state != FUNDING_UNAVAILABLE else None
    )
    summary["funding_evidence_state"] = funding_state
    summary["funding_evidence_error"] = funding_error
    summary["funding_required_settlements"] = len(required_boundaries)
    summary["funding_settlement_count"] = len(settlements)
    summary["funding_cashflow_count"] = len(cashflows)
    summary["selection_return_basis"] = RETURN_BASIS
    summary["legacy_cumulative_return_bps"] = summary.get("net_return_bps")
    slices = [
        item.to_dict()
        | {
            "scenario": scenario.name,
            "notional_usd": str(notional),
            "selection_return_basis": RETURN_BASIS,
            "funding_evidence_state": funding_state,
        }
        for item in sim.realized_slices
    ]
    return summary, slices


def _screen_rank(row: dict[str, object]) -> tuple[Decimal, int, Decimal]:
    return (
        D(str(row.get("selection_return_bps") or "0")),
        int(row.get("completed_round_trips") or 0),
        D(str(row.get("completed_round_trip_net_pnl_usd") or "0")),
    )


def _read_last_prospective_measurement(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    count = payload.get("prospective_shadow_count")
    if not isinstance(count, int) or isinstance(count, bool):
        return None
    return {
        "count": count,
        "challenger_queue_generated_at": payload.get("challenger_queue_generated_at"),
        "mode": payload.get("mode"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hlcopy.profitability.incremental_funnel_cli"
    )
    parser.add_argument("--wide-enriched-dir", required=True, type=Path)
    parser.add_argument("--wide-cutoff-ns-file", required=True, type=Path)
    parser.add_argument("--market-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--min-events", type=int, default=6)
    parser.add_argument("--min-confirm-events", type=int, default=3)
    parser.add_argument("--screen-limit", type=int, default=0)
    parser.add_argument("--confirm-top", type=int, default=40)
    parser.add_argument("--min-screen-actions", type=int, default=3)
    parser.add_argument("--taker-fee-bps", type=Decimal, default=D("4.5"))
    parser.add_argument("--max-slippage-bps", type=Decimal, default=D("20"))
    parser.add_argument("--max-book-forward-ms", type=int, default=750)
    parser.add_argument("--universe-state", type=Path, default=DEFAULT_UNIVERSE_STATE)
    parser.add_argument("--max-universe-age-hours", type=float, default=6.0)
    parser.add_argument("--prospective-report", type=Path, default=DEFAULT_PROSPECTIVE_REPORT)
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

    cohort_windows: list[
        tuple[str, str, tuple[CopyFillEvent, ...], tuple[CopyFillEvent, ...], str]
    ] = []
    min_screen = max(1, args.min_events)
    min_confirm = max(1, args.min_confirm_events)
    min_completed_round_trips = max(1, args.min_screen_actions)
    for (wallet, coin), raw_rows in grouped.items():
        all_rows = tuple(raw_rows)
        split = _split_oos(
            all_rows,
            min_screen_events=min_screen,
            min_confirm_events=min_confirm,
        )
        if split is None:
            continue
        screen_events, confirm_events = split
        window_id = _window_id(wallet, coin, all_rows, len(screen_events), cutoff_ns)
        cohort_windows.append((wallet, coin, screen_events, confirm_events, window_id))

    cohort_windows.sort(key=lambda item: len(item[2]) + len(item[3]), reverse=True)
    if args.screen_limit > 0:
        cohort_windows = cohort_windows[: args.screen_limit]

    funding_event_groups = [
        event_group
        for _wallet, _coin, screen_events, confirm_events, _window_id_value in cohort_windows
        for event_group in (screen_events, confirm_events)
    ]
    official_funding, funding_fetch_errors = asyncio.run(
        fetch_official_funding_history(funding_ranges_for_event_groups(funding_event_groups))
    )

    screen_path = args.output_dir / "screening.jsonl"
    confirm_path = args.output_dir / "confirmation.jsonl"
    slice_path = args.output_dir / "realized_slices.jsonl"
    report_path = args.output_dir / "funnel_report.json"

    active_windows = {window_id for *_, window_id in cohort_windows}
    screened = [
        row
        for row in _load_jsonl(screen_path)
        if str(row.get("window_id", "")) in active_windows
        and row.get("selection_return_basis") == RETURN_BASIS
    ]
    screened_keys = {
        _screen_key(
            str(row["wallet_address"]),
            str(row["coin"]),
            str(row["window_id"]),
        )
        for row in screened
        if row.get("window_id")
    }

    print(
        f"funnel_screen_start cohorts={len(cohort_windows)} already={len(screened_keys)} "
        f"scenario={SCREEN_SCENARIO.name} notional={SCREEN_NOTIONAL}",
        flush=True,
    )

    for index, (wallet, coin, screen_events, confirm_events, window_id) in enumerate(
        cohort_windows, 1
    ):
        key = _screen_key(wallet, coin, window_id)
        if key in screened_keys:
            continue
        summary, _ = _simulate(
            screen_events,
            market_dir=args.market_dir,
            scenario=SCREEN_SCENARIO,
            notional=SCREEN_NOTIONAL,
            taker_fee_bps=args.taker_fee_bps,
            max_slippage_bps=args.max_slippage_bps,
            max_book_forward_ms=args.max_book_forward_ms,
            funding_history_rows=official_funding.get(coin, []),
            funding_fetch_error=funding_fetch_errors.get(coin),
        )
        row = summary | {
            "coin": coin,
            "screen_event_count": len(screen_events),
            "held_out_event_count": len(confirm_events),
            "screen_end_received_ns": screen_events[-1].received_at_ns,
            "confirm_start_received_ns": confirm_events[0].received_at_ns,
            "window_id": window_id,
            "checkpoint_key": key,
        }
        screened.append(row)
        screened_keys.add(key)
        print(
            f"screen {index}/{len(cohort_windows)} wallet={wallet[:14]} coin={coin} "
            f"screen_events={len(screen_events)} held_out={len(confirm_events)} "
            f"funding={row['funding_evidence_state']} "
            f"round_trips={row['completed_round_trips']} "
            f"selection_return_bps={row['selection_return_bps']}",
            flush=True,
        )
    _write_jsonl_atomic(screen_path, screened)

    positive = [
        row
        for row in screened
        if row.get("funding_evidence_state") != FUNDING_UNAVAILABLE
        and int(row.get("completed_round_trips") or 0) >= min_completed_round_trips
        and row.get("selection_return_bps") is not None
        and D(str(row["selection_return_bps"])) > ZERO
    ]
    positive.sort(key=_screen_rank, reverse=True)
    finalists = positive[: max(1, args.confirm_top)]

    by_window = {
        (_cohort_key(wallet, coin), window_id): confirm_events
        for wallet, coin, _screen_events, confirm_events, window_id in cohort_windows
    }
    finalist_windows = {str(row["window_id"]) for row in finalists}
    confirmed = [
        row
        for row in _load_jsonl(confirm_path)
        if str(row.get("window_id", "")) in finalist_windows
        and row.get("selection_return_basis") == RETURN_BASIS
    ]
    confirmed_keys = {
        _confirmation_key(
            str(row["wallet_address"]),
            str(row["coin"]),
            str(row["scenario"]),
            str(row["notional_usd"]),
            str(row["window_id"]),
        )
        for row in confirmed
        if row.get("window_id")
    }
    realized_slices = [
        row
        for row in _load_jsonl(slice_path)
        if str(row.get("window_id", "")) in finalist_windows
        and row.get("selection_return_basis") == RETURN_BASIS
    ]

    print(
        f"funnel_confirm_start positive={len(positive)} finalists={len(finalists)} "
        f"existing_rows={len(confirmed_keys)}",
        flush=True,
    )

    for finalist in finalists:
        wallet = str(finalist["wallet_address"]).lower()
        coin = str(finalist["coin"])
        window_id = str(finalist["window_id"])
        confirm_events = by_window.get((_cohort_key(wallet, coin), window_id))
        if not confirm_events:
            continue
        for scenario in SCENARIOS:
            for notional in NOTIONALS:
                key = _confirmation_key(wallet, coin, scenario.name, str(notional), window_id)
                if key in confirmed_keys:
                    continue
                summary, slices = _simulate(
                    confirm_events,
                    market_dir=args.market_dir,
                    scenario=scenario,
                    notional=notional,
                    taker_fee_bps=args.taker_fee_bps,
                    max_slippage_bps=args.max_slippage_bps,
                    max_book_forward_ms=args.max_book_forward_ms,
                    funding_history_rows=official_funding.get(coin, []),
                    funding_fetch_error=funding_fetch_errors.get(coin),
                )
                row = summary | {
                    "coin": coin,
                    "window_id": window_id,
                    "confirmation_event_count": len(confirm_events),
                    "checkpoint_key": key,
                }
                confirmed.append(row)
                for item in slices:
                    realized_slices.append(
                        item
                        | {
                            "wallet_address": wallet,
                            "coin": coin,
                            "window_id": window_id,
                        }
                    )
                confirmed_keys.add(key)
                print(
                    f"confirm wallet={wallet[:14]} coin={coin} scenario={scenario.name} "
                    f"notional={notional} funding={row['funding_evidence_state']} "
                    f"round_trips={row['completed_round_trips']} "
                    f"selection_return_bps={row['selection_return_bps']}",
                    flush=True,
                )
    _write_jsonl_atomic(confirm_path, confirmed)
    _write_jsonl_atomic(slice_path, realized_slices)

    confirmed_by_window: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in confirmed:
        confirmed_by_window[
            (
                _cohort_key(str(row["wallet_address"]), str(row["coin"])),
                str(row["window_id"]),
            )
        ].append(row)

    robust: list[dict[str, object]] = []
    for finalist in finalists:
        wallet = str(finalist["wallet_address"])
        coin = str(finalist["coin"])
        window_id = str(finalist["window_id"])
        rows = confirmed_by_window.get((_cohort_key(wallet, coin), window_id), [])
        by_notional: dict[str, list[dict[str, object]]] = defaultdict(list)
        for row in rows:
            by_notional[str(row["notional_usd"])].append(row)
        for notional, scenario_rows in by_notional.items():
            if len({str(row["scenario"]) for row in scenario_rows}) != len(SCENARIOS):
                continue
            if any(
                row.get("funding_evidence_state") == FUNDING_UNAVAILABLE
                for row in scenario_rows
            ):
                continue
            returns = [
                D(str(row["selection_return_bps"]))
                for row in scenario_rows
                if row.get("selection_return_bps") is not None
            ]
            if len(returns) != len(SCENARIOS):
                continue
            worst = min(returns)
            actions = min(int(row["realized_actions"]) for row in scenario_rows)
            round_trips = min(int(row.get("completed_round_trips") or 0) for row in scenario_rows)
            if worst <= ZERO or round_trips < min_completed_round_trips:
                continue
            robust.append(
                {
                    "wallet_address": wallet,
                    "coin": coin,
                    "notional_usd": notional,
                    "worst_latency_return_bps": str(worst),
                    "actions_floor": actions,
                    "completed_round_trips_floor": round_trips,
                    "selection_return_basis": RETURN_BASIS,
                    "oos_window_id": window_id,
                }
            )

    robust.sort(
        key=lambda row: (
            D(str(row["worst_latency_return_bps"])),
            int(row["completed_round_trips_floor"]),
        ),
        reverse=True,
    )
    robust_cohorts = {
        _cohort_key(str(row["wallet_address"]), str(row["coin"])) for row in robust
    }
    screen_funding_unavailable = sum(
        1 for row in screened if row.get("funding_evidence_state") == FUNDING_UNAVAILABLE
    )
    confirmation_funding_unavailable = sum(
        1 for row in confirmed if row.get("funding_evidence_state") == FUNDING_UNAVAILABLE
    )
    report: dict[str, object] = {
        "mode": "INCREMENTAL_PROFITABILITY_FUNNEL_V4_DISJOINT_FUNDING_ADJUSTED",
        "real_trading": False,
        "return_basis": RETURN_BASIS,
        "selection_evidence_unit": "COMPLETED_FOLLOWER_ROUND_TRIP_AFTER_FUNDING",
        "funding_fetch_errors": funding_fetch_errors,
        "wide_event_count": len(events),
        "eligible_disjoint_cohort_count": len(cohort_windows),
        "screened_cohort_count": len(screened),
        "positive_screen_count": len(positive),
        "confirmed_row_count": len(confirmed),
        "robust_candidate_count": len(robust),
        "robust_candidates": robust[:100],
        "run_at": datetime.now(UTC).isoformat(),
        "oos_policy": {
            "screen_fraction_target": 0.60,
            "min_screen_events": min_screen,
            "min_confirm_events": min_confirm,
            "min_completed_round_trips": min_completed_round_trips,
            "screen_and_confirmation_disjoint": True,
            "stale_window_rows_expired": True,
            "stale_return_basis_rows_expired": True,
            "funding_evidence_fail_closed": True,
        },
        "boundary_counts": {
            "fetched": int(universe_payload.get("screened_wallets", 0)),
            "new_or_changed": len(universe_payload.get("registered_this_run", []))
            + len(universe_payload.get("refreshed_this_run", [])),
            "profiled": len(grouped),
            "screened": len(screened),
            "robust": len(robust),
        },
        "rejection_counts": {
            "insufficient_disjoint_events": len(grouped) - len(cohort_windows),
            "screen_funding_evidence_unavailable": screen_funding_unavailable,
            "screen_non_positive_or_too_few_round_trips": (
                len(screened) - len(positive) - screen_funding_unavailable
            ),
            "confirmation_funding_evidence_unavailable_rows": confirmation_funding_unavailable,
            "confirmation_not_robust": len(finalists)
            - len(
                {
                    _cohort_key(str(row["wallet_address"]), str(row["coin"]))
                    for row in finalists
                    if _cohort_key(str(row["wallet_address"]), str(row["coin"]))
                    in robust_cohorts
                }
            ),
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

    measured = _read_last_prospective_measurement(args.prospective_report)
    report["boundary_counts"]["prospective_shadow"] = measured["count"] if measured else None
    report["prospective_shadow_measurement"] = (
        measured
        if measured
        else {
            "count": None,
            "source": str(args.prospective_report),
            "status": "NO_MEASURED_PROSPECTIVE_REPORT",
        }
    )

    fd, temporary = tempfile.mkstemp(
        prefix=f".{report_path.name}.",
        dir=report_path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, report_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

    print(
        f"funnel_done screened={len(screened)} positive={len(positive)} "
        f"confirmed={len(confirmed)} robust={len(robust)} "
        f"challenger={counts['challenger']} "
        f"prospective_shadow_measured={report['boundary_counts']['prospective_shadow']} "
        f"rejected={counts['rejected']} demoted={counts['demoted']} "
        f"timestamp={queue['generated_at']} report={report_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
