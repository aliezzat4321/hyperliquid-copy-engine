from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg

from hlcopy.config import Settings
from hlcopy.market.symbols import canonical_coin
from hlcopy.research.cohort import CohortPolicy, apply_cohort, plan_cohort
from hlcopy.shadow.registry import WalletRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.research.cohort_cli")
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--max-validation-wallets", type=int, default=6)
    parser.add_argument("--max-seed-coins", type=int, default=6)
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

    selected_addresses = [row.address for row in plan if row.selected]
    seeds = _seed_coins(selected_addresses, max(0, args.max_seed_coins))
    result = apply_cohort(
        parquet_path=args.artifact,
        registry=WalletRegistry(args.registry),
        policy=policy,
        seed_coins_by_address=seeds,
    )
    print(
        json.dumps(
            {
                "selected_addresses": [row.address for row in result.selected],
                "promoted_ids": list(result.promoted_ids),
                "already_validation_ids": list(result.already_validation_ids),
                "remaining_capacity": result.remaining_capacity,
                "seed_coins": {address: list(coins) for address, coins in seeds.items()},
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
