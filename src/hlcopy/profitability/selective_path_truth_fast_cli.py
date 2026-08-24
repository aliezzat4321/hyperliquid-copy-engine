from __future__ import annotations

import asyncio
import json
import os
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from typing import Any

from hlcopy.profitability.champion_truth import REQUIRED_TRUTH_LAYERS
from hlcopy.profitability.continuous_path_v2 import AssetContextMark, FundingRate
from hlcopy.profitability.parquet_mark_stream import (
    iter_asset_context_marks,
    latest_asset_context_ns,
)
from hlcopy.profitability.parquet_stream_evaluator import (
    evaluate_candidate_path_truth_from_factory,
)
from hlcopy.profitability.path_inputs import (
    load_funding_history_jsonl,
    load_margin_snapshots_jsonl,
)
from hlcopy.profitability.portfolio_position_copy import FollowerStateEvent
from hlcopy.profitability.selective_path_truth_cli import (
    LEVERAGES,
    MIN_FORWARD_ACTIONS,
    D,
    _group_key,
    _index_by_coin,
    _promotion_groups,
    _refresh_funding,
    _state,
    _summary_index,
    build_parser,
)

MARK_LOOKBACK_NS = 15_000_000_000


def _active_intervals(
    states: list[FollowerStateEvent], end_ns: int
) -> dict[str, tuple[tuple[int, int], ...]]:
    by_coin: dict[str, list[FollowerStateEvent]] = defaultdict(list)
    for state in states:
        by_coin[state.coin].append(state)

    result: dict[str, tuple[tuple[int, int], ...]] = {}
    for coin, rows in by_coin.items():
        ordered = sorted(
            rows,
            key=lambda row: (
                row.execution_received_at_ns,
                row.source_tid,
                row.action,
            ),
        )
        intervals: list[tuple[int, int]] = []
        opened_at: int | None = None
        previous_qty = D("0")
        for row in ordered:
            now_ns = row.execution_received_at_ns
            if previous_qty == 0 and row.qty_after != 0:
                opened_at = now_ns
            if previous_qty != 0 and row.qty_after == 0 and opened_at is not None:
                intervals.append((opened_at, now_ns))
                opened_at = None
            previous_qty = row.qty_after
        if opened_at is not None:
            intervals.append((opened_at, end_ns))
        if intervals:
            result[coin] = tuple(intervals)
    return result


def _marks_for_active_intervals(
    *,
    marks_by_coin: dict[str, tuple[AssetContextMark, ...]],
    intervals: dict[str, tuple[tuple[int, int], ...]],
) -> tuple[AssetContextMark, ...]:
    """Legacy in-memory helper retained for regression tests only."""
    selected: dict[tuple[str, int], AssetContextMark] = {}
    for coin, spans in intervals.items():
        rows = marks_by_coin.get(coin, ())
        if not rows:
            continue
        times = tuple(row.received_at_ns for row in rows)
        for start_ns, end_ns in spans:
            left = max(0, bisect_right(times, start_ns) - 1)
            right = min(len(rows), bisect_right(times, end_ns) + 1)
            for row in rows[left:right]:
                selected[(row.coin, row.received_at_ns)] = row
    return tuple(
        sorted(selected.values(), key=lambda row: (row.received_at_ns, row.coin))
    )


def _funding_for_active_intervals(
    *,
    funding_by_coin: dict[str, tuple[FundingRate, ...]],
    intervals: dict[str, tuple[tuple[int, int], ...]],
) -> tuple[FundingRate, ...]:
    selected: dict[tuple[str, int], FundingRate] = {}
    for coin, spans in intervals.items():
        rows = funding_by_coin.get(coin, ())
        if not rows:
            continue
        times_ns = tuple(row.payment_ts_ms * 1_000_000 for row in rows)
        for start_ns, end_ns in spans:
            left = bisect_left(times_ns, start_ns)
            right = bisect_right(times_ns, end_ns)
            for row in rows[left:right]:
                selected[(row.coin, row.payment_ts_ms)] = row
    return tuple(
        sorted(selected.values(), key=lambda row: (row.payment_ts_ms, row.coin))
    )


def _deferred_truth_payload(*, fee_complete: bool) -> dict[str, object]:
    blockers = [
        layer
        for layer in REQUIRED_TRUTH_LAYERS
        if layer != "round_trip_fee_accounting" or not fee_complete
    ]
    return {
        "coverage": {
            "complete": False,
            "blockers": ["DEFERRED_UNTIL_MIN_FORWARD_ACTIONS"],
            "checkpoint_count": 0,
            "applied_funding_count": 0,
        },
        "safe_leverage": None,
        "validation_status": "BLOCKED_INCOMPLETE_PROFITABILITY_OR_PATH_RISK_TRUTH",
        "validation_blockers": blockers,
        "required_truth_layers": list(REQUIRED_TRUTH_LAYERS),
        "gap_diagnostics": {},
    }


async def _run(args: Any) -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("selective path truth refuses REAL_TRADING_ENABLED=YES")

    payload = json.loads(args.state_events.read_text(encoding="utf-8"))
    if payload.get("real_trading") is not False:
        raise SystemExit("selective state artifact must be research-only")
    fee_complete = (
        payload.get("fee_accounting_mode") == "ALLOCATED_ENTRY_PLUS_EXIT_FEES_V1"
    )
    raw_rows = [
        row for row in payload.get("state_events") or [] if isinstance(row, dict)
    ]
    master = json.loads(args.master.read_text(encoding="utf-8"))
    summaries = _summary_index(master)

    groups: dict[tuple[str, str, str, str], list[FollowerStateEvent]] = defaultdict(list)
    for raw in raw_rows:
        groups[_group_key(raw)].append(_state(raw))

    mature_keys = {
        key
        for key in groups
        if int((summaries.get(key) or {}).get("realized_actions") or 0)
        >= MIN_FORWARD_ACTIONS
    }
    mature_states = [state for key in mature_keys for state in groups[key]]
    first_state_ms: dict[str, int] = {}
    for state in mature_states:
        first_state_ms[state.coin] = min(
            first_state_ms.get(state.coin, state.execution_ts_ms),
            state.execution_ts_ms,
        )

    coins = tuple(sorted(first_state_ms))
    start_ns = min(
        (state.execution_received_at_ns for state in mature_states),
        default=None,
    )
    tape_start_ns = max(0, start_ns - MARK_LOOKBACK_NS) if start_ns is not None else None
    end_ns = latest_asset_context_ns(
        args.market_dir,
        coins=coins,
        start_ns=tape_start_ns,
    ) or 0
    if end_ns == 0:
        end_ns = max(
            (
                state.execution_received_at_ns
                for rows in groups.values()
                for state in rows
            ),
            default=0,
        )
    end_ms = end_ns // 1_000_000

    if coins and end_ms > 0:
        await _refresh_funding(
            coins=coins,
            first_state_ms=first_state_ms,
            end_ms=end_ms,
            cache_path=args.funding_cache,
        )

    funding: tuple[FundingRate, ...] = ()
    if args.funding_cache.exists() and coins:
        funding = load_funding_history_jsonl(args.funding_cache, coins=coins)
    funding_by_coin = _index_by_coin(funding)
    margins = load_margin_snapshots_jsonl(args.margin_snapshots)

    rows_out: list[dict[str, object]] = []
    ordered_groups = sorted(groups.items())
    total_groups = len(ordered_groups)
    mature_total = len(mature_keys)
    evaluated = 0
    deferred = 0
    started = time.monotonic()

    print(
        "selective_path_truth_plan "
        f"groups={total_groups} mature={mature_total} deferred={total_groups - mature_total} "
        f"mature_coins={len(coins)} funding={len(funding)} mode=PARQUET_STREAM_V4_1",
        flush=True,
    )

    for group_number, (key, states) in enumerate(ordered_groups, start=1):
        lane, wallet, scenario, notional = key
        source = summaries.get(key, {})
        realized_actions = int(source.get("realized_actions") or 0)
        first_ns = min(state.execution_received_at_ns for state in states)
        net_return_bps = source.get("net_return_bps")

        if key not in mature_keys:
            deferred += 1
            truth_payload = _deferred_truth_payload(fee_complete=fee_complete)
            path_truth_complete = False
        else:
            intervals = _active_intervals(states, end_ns)
            group_funding = _funding_for_active_intervals(
                funding_by_coin=funding_by_coin,
                intervals=intervals,
            )
            group_coins = tuple(sorted({state.coin for state in states}))
            group_start_ns = max(0, first_ns - MARK_LOOKBACK_NS)

            def mark_factory(
                *,
                _coins: tuple[str, ...] = group_coins,
                _start_ns: int = group_start_ns,
                _end_ns: int = end_ns,
            ):
                return iter_asset_context_marks(
                    args.market_dir,
                    coins=_coins,
                    start_ns=_start_ns,
                    end_ns=_end_ns,
                )

            truth, gap_diagnostics = evaluate_candidate_path_truth_from_factory(
                state_events=states,
                mark_factory=mark_factory,
                funding_rates=group_funding,
                margin_snapshots=margins,
                leverages=LEVERAGES,
                round_trip_fee_accounting=fee_complete,
            )
            truth_payload = truth.to_dict()
            truth_payload["gap_diagnostics"] = gap_diagnostics
            path_truth_complete = bool(
                truth_payload.pop("validated_champion", False)
            )
            evaluated += 1
            coverage = truth_payload.get("coverage") or {}
            print(
                "selective_path_truth_mature "
                f"evaluated={evaluated}/{mature_total} group={group_number}/{total_groups} "
                f"elapsed_s={time.monotonic() - started:.1f} "
                f"wallet={wallet[:14]} scenario={scenario} notional={notional} "
                f"actions={realized_actions} checkpoints={coverage.get('checkpoint_count', 0)} "
                f"gaps={len(gap_diagnostics)}",
                flush=True,
            )

        rows_out.append(
            {
                "lane": lane,
                "wallet_address": wallet,
                "scenario": scenario,
                "notional_usd": notional,
                "realized_actions": realized_actions,
                "closed_net_pnl_usd": source.get("closed_net_pnl_usd"),
                "avg_net_pnl_usd": source.get("avg_net_pnl_usd"),
                "net_return_bps": net_return_bps,
                "net_return_pct": (
                    str(D(str(net_return_bps)) / D("100"))
                    if net_return_bps is not None
                    else None
                ),
                "execution_pct": source.get("execution_pct"),
                "evidence_age_ns": max(0, end_ns - first_ns),
                "path_truth_complete": path_truth_complete,
                **truth_payload,
            }
        )

    promotion = _promotion_groups(rows_out)
    rows_out.sort(
        key=lambda row: (
            bool(row.get("path_truth_complete")),
            int(row.get("realized_actions") or 0),
            D(str(row.get("closed_net_pnl_usd") or "0")),
        ),
        reverse=True,
    )
    result = {
        "generated_at_ns": time.time_ns(),
        "real_trading": False,
        "mode": "PROSPECTIVE_SELECTIVE_SHADOW_PATH_TRUTH_V4_1_PARQUET_STREAM",
        "policy_id": payload.get("latest_policy_id"),
        "fee_accounting_complete": fee_complete,
        "minimum_forward_actions": MIN_FORWARD_ACTIONS,
        "minimum_forward_hours": "24",
        "state_event_count": len(raw_rows),
        "mark_count": None,
        "mark_count_mode": "STREAMED_NOT_MATERIALIZED",
        "funding_count": len(funding),
        "margin_snapshot_count": len(margins),
        "scenario_candidate_count": len(rows_out),
        "path_evaluated_candidate_count": evaluated,
        "path_deferred_candidate_count": deferred,
        "promotion_candidate_count": len(promotion),
        "validated_champion_count": sum(
            bool(row["validated_champion"]) for row in promotion
        ),
        "promotion_candidates": promotion,
        "scenario_candidates": rows_out,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    tmp.replace(args.output)
    print(
        "selective_path_truth "
        f"candidates={len(promotion)} champions={result['validated_champion_count']} "
        f"evaluated={evaluated} deferred={deferred} "
        f"states={len(raw_rows)} funding={len(funding)} "
        f"margin_snapshots={len(margins)} output={args.output}",
        flush=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
