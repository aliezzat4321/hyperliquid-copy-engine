from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from hlcopy.research.cohort import CohortPolicy, apply_cohort, plan_cohort
from hlcopy.shadow.registry import WalletRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.research.cohort_cli")
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--max-validation-wallets", type=int, default=6)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    sub.add_parser("apply")
    return parser


def _policy(args: argparse.Namespace) -> CohortPolicy:
    return CohortPolicy(max_validation_wallets=max(1, args.max_validation_wallets))


def main() -> None:
    args = build_parser().parse_args()
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("validation cohort selector refuses REAL_TRADING_ENABLED=YES")
    policy = _policy(args)
    if args.command == "plan":
        rows = plan_cohort(args.artifact, policy)
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
                    for row in rows
                ],
                sort_keys=True,
            )
        )
        return

    result = apply_cohort(
        parquet_path=args.artifact,
        registry=WalletRegistry(args.registry),
        policy=policy,
    )
    print(
        json.dumps(
            {
                "selected_addresses": [row.address for row in result.selected],
                "promoted_ids": list(result.promoted_ids),
                "already_validation_ids": list(result.already_validation_ids),
                "remaining_capacity": result.remaining_capacity,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
