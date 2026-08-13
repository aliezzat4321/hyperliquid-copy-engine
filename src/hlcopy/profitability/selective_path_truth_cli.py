from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from hlcopy.config import Settings
from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.market.symbols import canonical_coin, wire_coin
from hlcopy.profitability.continuous_path_v2 import FundingRate
from hlcopy.profitability.path_inputs import (
    load_asset_context_marks,
    load_funding_history_jsonl,
    load_margin_snapshots_jsonl,
)
from hlcopy.profitability.path_truth import evaluate_candidate_path_truth
from hlcopy.profitability.portfolio_position_copy import FollowerStateEvent

D = Decimal
LEVERAGES = tuple(D(value) for value in ("1", "2", "3", "5", "7.5", "10", "15", "20", "25", "40", "50"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.profitability.selective_path_truth_cli")
    parser.add_argument("--state-events", required=True, type=Path)
    parser.add_argument("--master", required=True, type=Path)
    parser.add_argument("--market-dir", required=True, type=Path)
    parser.add_argument("--margin-snapshots", required=True, type=Path)
    parser.add_argument("--funding-cache", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _state(row: dict[str, Any]) -> FollowerStateEvent:
    raw_entry = row.get("avg_entry_after")
    return FollowerStateEvent(
        coin=canonical_coin(row["coin"]),
        execution_ts_ms=int(row["execution_ts_ms"]),
        execution_received_at_ns=int(row["execution_received_at_ns"]),
        source_tid=int(row["source_tid"]),
        action=str(row["action"]),
        qty_after=D(str(row["qty_after"])),
        avg_entry_after=None if raw_entry is None else D(str(raw_entry)),
        realized_net_pnl_cumulative_usd=D(str(row["realized_net_pnl_cumulative_usd"])),
        entry_fee_remaining_usd=D(str(row["entry_fee_remaining_usd"])),
    )


def _group_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("lane") or "UNKNOWN"),
        str(row.get("wallet_address") or "").lower(),
        str(row.get("scenario") or "UNKNOWN"),
        str(row.get("notional_usd") or "0"),
    )


def _latest_cached_funding(path: Path) -> dict[str, int]:
    latest: dict[str, int] = {}
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            outer = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows = outer.get("rows") if isinstance(outer, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            coin = canonical_coin(row.get("coin") or "")
            try:
                ts = int(row.get("time"))
            except (TypeError, ValueError):
                continue
            if coin:
                latest[coin] = max(latest.get(coin, 0), ts)
    return latest


async def _refresh_funding(
    *,
    coins: tuple[str, ...],
    first_state_ms: dict[str, int],
    end_ms: int,
    cache_path: Path,
) -> None:
    if not coins:
        return
    latest = _latest_cached_funding(cache_path)
    settings = Settings.from_env()
    records: list[dict[str, object]] = []
    async with HyperliquidHttpClient(
        settings.api_url,
        settings.leaderboard_url,
        concurrency=settings.http_concurrency,
    ) as client:
        for coin in coins:
            start_ms = max(first_state_ms[coin], latest.get(coin, first_state_ms[coin] - 1) + 1)
            if start_ms > end_ms:
                continue
            pages = await client.funding_history_by_time(
                wire_coin(coin),
                start_ms,
                end_ms,
            )
            normalized: list[dict[str, object]] = []
            for page in pages:
                raw_rows = page.response_payload
                if not isinstance(raw_rows, list):
                    continue
                for raw in raw_rows:
                    if not isinstance(raw, dict):
                        continue
                    try:
                        ts = int(raw.get("time"))
                        rate = str(raw["fundingRate"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if ts < start_ms or ts > end_ms:
                        continue
                    normalized.append(
                        {
                            **raw,
                            "coin": coin,
                            "time": ts,
                            "fundingRate": rate,
                        }
                    )
            if normalized:
                records.append(
                    {
                        "fetched_at_ns": time.time_ns(),
                        "network": settings.network,
                        "coin": coin,
                        "wire_coin": wire_coin(coin),
                        "start_time_ms": start_ms,
                        "end_time_ms": end_ms,
                        "rows": normalized,
                    }
                )
    if not records:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _summary_index(master: dict[str, Any]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    out: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in master.get("summaries") or []:
        if not isinstance(row, dict):
            continue
        key = (
            str(row.get("lane") or "UNKNOWN"),
            str(row.get("wallet_address") or "").lower(),
            str(row.get("scenario") or "UNKNOWN"),
            str(row.get("notional_usd") or "0"),
        )
        out[key] = row
    return out


async def _run(args: argparse.Namespace) -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("selective path truth refuses REAL_TRADING_ENABLED=YES")
    payload = json.loads(args.state_events.read_text(encoding="utf-8"))
    if payload.get("real_trading") is not False:
        raise SystemExit("selective state artifact must be research-only")
    fee_complete = payload.get("fee_accounting_mode") == "ALLOCATED_ENTRY_PLUS_EXIT_FEES_V1"
    raw_rows = [row for row in payload.get("state_events") or [] if isinstance(row, dict)]
    master = json.loads(args.master.read_text(encoding="utf-8"))
    summaries = _summary_index(master)

    groups: dict[tuple[str, str, str, str], list[FollowerStateEvent]] = defaultdict(list)
    first_state_ms: dict[str, int] = {}
    for raw in raw_rows:
        event = _state(raw)
        groups[_group_key(raw)].append(event)
        first_state_ms[event.coin] = min(
            first_state_ms.get(event.coin, event.execution_ts_ms),
            event.execution_ts_ms,
        )

    coins = tuple(sorted(first_state_ms))
    start_ns = min((event.execution_received_at_ns for rows in groups.values() for event in rows), default=None)
    marks = load_asset_context_marks(args.market_dir, coins=coins, start_ns=start_ns)
    end_ns = max((mark.received_at_ns for mark in marks), default=start_ns or 0)
    end_ms = end_ns // 1_000_000
    if coins and end_ms > 0:
        await _refresh_funding(
            coins=coins,
            first_state_ms=first_state_ms,
            end_ms=end_ms,
            cache_path=args.funding_cache,
        )

    funding: tuple[FundingRate, ...] = ()
    if args.funding_cache.exists():
        funding = load_funding_history_jsonl(args.funding_cache, coins=coins)
    margins = load_margin_snapshots_jsonl(args.margin_snapshots)

    rows_out: list[dict[str, object]] = []
    for key, states in sorted(groups.items()):
        lane, wallet, scenario, notional = key
        group_coins = {event.coin for event in states}
        first_ns = min(event.execution_received_at_ns for event in states)
        group_marks = tuple(
            mark for mark in marks if mark.coin in group_coins and mark.received_at_ns >= first_ns
        )
        first_ms = first_ns // 1_000_000
        group_funding = tuple(
            row for row in funding if row.coin in group_coins and row.payment_ts_ms >= first_ms
        )
        truth = evaluate_candidate_path_truth(
            state_events=states,
            asset_contexts=group_marks,
            funding_rates=group_funding,
            margin_snapshots=margins,
            leverages=LEVERAGES,
            round_trip_fee_accounting=fee_complete,
        )
        source = summaries.get(key, {})
        rows_out.append(
            {
                "lane": lane,
                "wallet_address": wallet,
                "scenario": scenario,
                "notional_usd": notional,
                "realized_actions": int(source.get("realized_actions") or 0),
                "closed_net_pnl_usd": source.get("closed_net_pnl_usd"),
                "avg_net_pnl_usd": source.get("avg_net_pnl_usd"),
                "net_return_bps": source.get("net_return_bps"),
                "execution_pct": source.get("execution_pct"),
                **truth.to_dict(),
            }
        )

    rows_out.sort(
        key=lambda row: (
            bool(row.get("validated_champion")),
            int(row.get("realized_actions") or 0),
            D(str(row.get("closed_net_pnl_usd") or "0")),
        ),
        reverse=True,
    )
    result = {
        "generated_at_ns": time.time_ns(),
        "real_trading": False,
        "mode": "PROSPECTIVE_SELECTIVE_SHADOW_PATH_TRUTH_V1",
        "policy_id": payload.get("latest_policy_id"),
        "fee_accounting_complete": fee_complete,
        "state_event_count": len(raw_rows),
        "mark_count": len(marks),
        "funding_count": len(funding),
        "margin_snapshot_count": len(margins),
        "candidate_count": len(rows_out),
        "validated_champion_count": sum(bool(row.get("validated_champion")) for row in rows_out),
        "candidates": rows_out,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    tmp.replace(args.output)
    print(
        "selective_path_truth "
        f"candidates={len(rows_out)} champions={result['validated_champion_count']} "
        f"states={len(raw_rows)} marks={len(marks)} funding={len(funding)} "
        f"margin_snapshots={len(margins)} output={args.output}",
        flush=True,
    )


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
