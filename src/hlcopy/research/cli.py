from __future__ import annotations

import argparse
import os
from dataclasses import replace
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.pipeline import run as run_pipeline
from hlcopy.research.publisher import publish_ranked_candidates
from hlcopy.research.selective_policy_publisher import publish_policy_from_attribution
from hlcopy.shadow.registry import WalletRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.research.cli")
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--max-candidates", type=int)
    parser.add_argument("--max-publish", type=int, default=25)
    parser.add_argument("--min-composite-score", type=float, default=0.0)
    parser.add_argument("--attribution", type=Path)
    parser.add_argument("--policy-store", type=Path)
    parser.add_argument("run", nargs="?")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("wallet research refuses to run with REAL_TRADING_ENABLED=YES")
    settings = Settings.from_env()
    if args.max_candidates is not None:
        settings = replace(settings, max_candidates=max(1, args.max_candidates))
    artifact = run_pipeline(settings)
    result = publish_ranked_candidates(
        parquet_path=artifact,
        registry=WalletRegistry(args.registry),
        ledger_path=args.ledger,
        max_candidates=max(0, args.max_publish),
        min_composite_score=args.min_composite_score,
    )
    print(
        "research publish "
        f"observed={result.observed} newly_registered={result.newly_registered} "
        f"existing={result.skipped_existing} fingerprint={result.artifact_fingerprint[:16]} "
        f"artifact={artifact}",
        flush=True,
    )
    if args.attribution is None and args.policy_store is None:
        return
    if args.attribution is None or args.policy_store is None:
        raise SystemExit("--attribution and --policy-store must be supplied together")
    if not args.attribution.exists():
        print("policy_publish skipped reason=ATTRIBUTION_MISSING", flush=True)
        return
    policy = publish_policy_from_attribution(
        attribution_path=args.attribution,
        policy_store_path=args.policy_store,
    )
    print(
        f"policy_publish published={policy.published} policy_id={policy.policy_id} "
        f"rules={policy.rules} newly_added={policy.newly_added_rules} "
        f"watch={policy.watch_rules} demoted={policy.demoted_rules} reason={policy.reason}",
        flush=True,
    )


if __name__ == "__main__":
    main()
