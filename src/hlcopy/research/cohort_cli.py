from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import psycopg

from hlcopy.config import Settings
from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.market.symbols import canonical_coin
from hlcopy.research.cohort import CohortPolicy, apply_cohort, plan_cohort
from hlcopy.shadow.registry import MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP, WalletRegistry

DEFAULT_MAX_SEED_COINS = 200
MAX_PREWARM_MARKETS = 200


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.research.cohort_cli")
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument(
        "--max-validation-wallets",
        type=int,
        default=MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP,
    )
    parser.add_argument(
        "--max-seed-coins",
        type=int,
        default=DEFAULT_MAX_SEED_COINS,
        help=(
            "maximum historical markets to prewarm per validation wallet; "
            "the shadow process enforces a separate global market-universe safety cap"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    sub.add_parser("apply")
    return parser


def _policy(args: argparse.Namespace) -> CohortPolicy:
    return CohortPolicy(max_validation_wallets=max(1, args.max_validation_wallets))


def _seed_coins(addresses: list[str], max_coins: int) -> dict[str, tuple[str, ...]]:
    if not addresses or max_coins <= 0:
        return {}
    result: dict[str, tuple[str, ...]] = {}
    settings = Settings.from_env()
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cursor:
            for address in addresses:
                cursor.execute(
                    """
                    SELECT coin, COUNT(*) AS fills, MAX(timestamp) AS latest
                    FROM fills
                    WHERE wallet_address = %s
                    GROUP BY coin
                    ORDER BY fills DESC, latest DESC, coin
                    LIMIT %s
                    """,
                    (address.lower(), max_coins),
                )
                result[address.lower()] = tuple(
                    canonical_coin(row[0]) for row in cursor.fetchall()
                )
    return result


def _parse_active_perp_markets(payload: Any) -> frozenset[str]:
    markets: set[str] = set()
    if not isinstance(payload, list):
        return frozenset()
    for item in payload:
        meta: Any = item
        if isinstance(item, list) and item:
            meta = item[0]
        if not isinstance(meta, dict):
            continue
        universe = meta.get("universe")
        if not isinstance(universe, list):
            continue
        for asset in universe:
            if not isinstance(asset, dict) or asset.get("isDelisted") is True:
                continue
            name = canonical_coin(asset.get("name"))
            if name:
                markets.add(name)
    return frozenset(markets)


async def _current_perp_markets() -> frozenset[str]:
    settings = Settings.from_env()
    async with HyperliquidHttpClient(
        settings.api_url,
        settings.leaderboard_url,
        concurrency=settings.http_concurrency,
    ) as client:
        response = await client.info({"type": "allPerpMetas"})
    markets = _parse_active_perp_markets(response.response_payload)
    if not markets:
        raise RuntimeError("allPerpMetas returned no active markets; refusing blind prewarm")
    return markets


def _filter_current_markets(
    seeds: dict[str, tuple[str, ...]],
    current_markets: frozenset[str],
) -> dict[str, tuple[str, ...]]:
    return {
        address: tuple(coin for coin in coins if coin in current_markets)
        for address, coins in seeds.items()
    }


def _active_validation_addresses(registry: WalletRegistry) -> list[str]:
    registry.init()
    return [
        wallet.source_ref.lower()
        for wallet in registry.load()
        if wallet.enabled
        and wallet.source_type == "hyperliquid_wallet"
        and wallet.stage in {"validation", "approved"}
    ]


def main() -> None:
    args = build_parser().parse_args()
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("validation cohort selector refuses REAL_TRADING_ENABLED=YES")
    policy = _policy(args)
    plan = plan_cohort(args.artifact, policy)
    if args.command == "plan":
        print(
            json.dumps(
                [
                    {
                        "address": row.address,
                        "rank": row.rank,
                        "selected": row.selected,
                        "rejection_reasons": list(row.rejection_reasons),
                        "composite_score": row.composite_score,
                        "copyability_score": row.copyability_score,
                        "confidence_score": row.confidence_score,
                        "trade_count": row.trade_count,
                    }
                    for row in plan
                ],
                sort_keys=True,
            )
        )
        return

    registry = WalletRegistry(args.registry)
    selected_addresses = [row.address for row in plan if row.selected]
    active_addresses = _active_validation_addresses(registry)
    seed_addresses = list(dict.fromkeys((*active_addresses, *selected_addresses)))
    historical_seeds = _seed_coins(seed_addresses, max(0, args.max_seed_coins))
    current_markets = asyncio.run(_current_perp_markets())
    seeds = _filter_current_markets(historical_seeds, current_markets)
    prewarm_union = tuple(dict.fromkeys(coin for coins in seeds.values() for coin in coins))
    if len(prewarm_union) > MAX_PREWARM_MARKETS:
        raise SystemExit(
            "validation prewarm exceeds safe single-shard market budget: "
            f"{len(prewarm_union)} > {MAX_PREWARM_MARKETS}; shard market capture before continuing"
        )
    result = apply_cohort(
        parquet_path=args.artifact,
        registry=registry,
        policy=policy,
        seed_coins_by_address=seeds,
    )
    historical_union = {
        coin for coins in historical_seeds.values() for coin in coins
    }
    print(
        json.dumps(
            {
                "selected_addresses": [row.address for row in result.selected],
                "promoted_ids": list(result.promoted_ids),
                "already_validation_ids": list(result.already_validation_ids),
                "remaining_capacity": result.remaining_capacity,
                "prewarmed_addresses": seed_addresses,
                "current_perp_market_count": len(current_markets),
                "historical_market_union_count": len(historical_union),
                "prewarm_market_union_count": len(prewarm_union),
                "dropped_noncurrent_markets": sorted(historical_union - current_markets),
                "seed_coins": {address: list(coins) for address, coins in seeds.items()},
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
