from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from hlcopy.analytics.performance import calculate_wallet_metrics
from hlcopy.config import Settings
from hlcopy.db.postgres import Database
from hlcopy.discovery.leaderboard import parse_leaderboard, shortlist
from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.models import Fill
from hlcopy.positions.reconstruction import reconstruct_positions
from hlcopy.positions.state_machine import PositionReconstructionError
from hlcopy.ranking.scores import RankedWallet, rank_wallet

RANKING_RULE_VERSION = "rank-wallet-v1"


async def run_pipeline(settings: Settings) -> Path:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    async with Database(settings.database_url) as db:
        await db.init_schema()
        async with HyperliquidHttpClient(
            settings.api_url,
            settings.leaderboard_url,
            concurrency=settings.http_concurrency,
        ) as client:
            leaderboard_response = await client.leaderboard()
            await db.store_raw(
                source="hyperliquid",
                endpoint=leaderboard_response.endpoint,
                request_payload=None,
                response_payload=leaderboard_response.response_payload,
                fetched_at_ms=leaderboard_response.fetched_at_ms,
            )
            candidates = parse_leaderboard(leaderboard_response.response_payload)
            await db.upsert_leaderboard(candidates, leaderboard_response.fetched_at_ms)
            selected = shortlist(
                candidates,
                limit=settings.max_candidates,
                min_account_value=settings.min_account_value,
                min_month_roi=settings.min_month_roi,
                min_month_volume=settings.min_month_volume,
            )

            ranked: list[RankedWallet] = []
            for idx, candidate in enumerate(selected, start=1):
                print(f"[{idx}/{len(selected)}] analyzing {candidate.address}", flush=True)
                response = await client.user_fills(candidate.address)
                await db.store_raw(
                    source="hyperliquid",
                    endpoint="userFills",
                    request_payload=response.request_payload,
                    response_payload=response.response_payload,
                    fetched_at_ms=response.fetched_at_ms,
                )
                rows = (
                    response.response_payload
                    if isinstance(response.response_payload, list)
                    else []
                )
                # Leaderboard research is perp-focused; userFills may also include spot.
                perp_rows = [
                    row
                    for row in rows
                    if "Long" in str(row.get("dir", ""))
                    or "Short" in str(row.get("dir", ""))
                ]
                fills = [Fill.from_raw(candidate.address, row) for row in perp_rows]
                fills.sort(key=lambda f: (f.timestamp_ms, f.tid))
                await db.upsert_fills(fills)
                if not fills:
                    continue
                try:
                    episodes, _states = reconstruct_positions(fills)
                except PositionReconstructionError as exc:
                    print(f"  data-quality rejection: {exc}", flush=True)
                    continue
                await db.replace_episodes(candidate.address, episodes)
                metrics = calculate_wallet_metrics(episodes, fills)
                await db.store_metrics(
                    candidate.address,
                    response.fetched_at_ms,
                    "recent_userFills_window",
                    metrics.to_dict(),
                )
                ranked.append(rank_wallet(candidate, metrics))

    ranked.sort(key=lambda item: item.composite_score, reverse=True)
    screened_count = len(candidates)
    shortlisted_count = len(selected)
    ranked_count = len(ranked)
    rows = [
        item.to_dict()
        | {
            "rank": idx,
            "source_snapshot_ms": leaderboard_response.fetched_at_ms,
            "screened_count": screened_count,
            "shortlisted_count": shortlisted_count,
            "ranked_count": ranked_count,
            "ranking_rule_version": RANKING_RULE_VERSION,
        }
        for idx, item in enumerate(ranked, start=1)
    ]
    frame = pl.DataFrame(rows) if rows else pl.DataFrame({"rank": [], "address": []})
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    csv_path = settings.output_dir / f"ranked_candidates_{stamp}.csv"
    parquet_path = settings.output_dir / f"ranked_candidates_{stamp}.parquet"
    frame.write_csv(csv_path)
    frame.write_parquet(parquet_path)
    print(
        "research snapshot "
        f"screened={screened_count} shortlisted={shortlisted_count} ranked={ranked_count} "
        f"as_of_ms={leaderboard_response.fetched_at_ms} rule={RANKING_RULE_VERSION}",
        flush=True,
    )
    print(f"wrote {csv_path}")
    print(f"wrote {parquet_path}")
    return parquet_path


def run(settings: Settings | None = None) -> Path:
    return asyncio.run(run_pipeline(settings or Settings.from_env()))
