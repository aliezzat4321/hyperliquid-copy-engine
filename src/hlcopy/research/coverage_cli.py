from __future__ import annotations

import argparse
import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.research.coverage import CoverageConfig, populate_external_evidence_coverage
from hlcopy.resolver.engine import ResolverConfig
from hlcopy.resolver.source_registry import ExternalSourceRegistry, ExternalSourceSpec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.research.coverage_cli")
    parser.add_argument("--source-registry", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan")
    scan.add_argument("--id", required=True)
    _add_options(scan)

    scan_all = sub.add_parser("scan-all")
    _add_options(scan_all)
    return parser


def _add_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--anchor-trades", type=int, default=16)
    parser.add_argument("--evidence-lookback-days", type=int, default=14)
    parser.add_argument("--time-tolerance-ms", type=int, default=5_000)
    parser.add_argument("--price-tolerance-bps", type=Decimal, default=Decimal("5"))
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--universe-limit", type=int, default=5_000)
    parser.add_argument("--min-account-value", type=float, default=0.0)
    parser.add_argument("--min-month-roi", type=float, default=0.0)
    parser.add_argument("--min-month-volume", type=float, default=0.0)


def _resolver_config(args: argparse.Namespace) -> ResolverConfig:
    return ResolverConfig(
        anchor_trades=max(6, args.anchor_trades),
        evidence_lookback_days=max(1, args.evidence_lookback_days),
        time_tolerance_ms=max(1, args.time_tolerance_ms),
        price_tolerance_bps=args.price_tolerance_bps,
        max_candidates=500,
        report_candidates=25,
    )


def _coverage_config(args: argparse.Namespace) -> CoverageConfig:
    return CoverageConfig(
        batch_size=max(1, args.batch_size),
        universe_limit=max(1, args.universe_limit),
        min_account_value=args.min_account_value,
        min_month_roi=args.min_month_roi,
        min_month_volume=args.min_month_volume,
    )


async def _scan_one(args: argparse.Namespace, source: ExternalSourceSpec) -> dict[str, object]:
    result = await populate_external_evidence_coverage(
        source=source,
        settings=Settings.from_env(),
        output_dir=args.output_dir,
        resolver_config=_resolver_config(args),
        coverage_config=_coverage_config(args),
    )
    return {
        "source_id": result.source_id,
        "scanned_this_run": result.scanned_this_run,
        "scanned_total": result.scanned_total,
        "universe_size": result.universe_size,
        "exhausted": result.exhausted,
        "state_path": result.state_path,
    }


async def _run(args: argparse.Namespace) -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("research coverage crawler refuses REAL_TRADING_ENABLED=YES")
    registry = ExternalSourceRegistry(args.source_registry)
    if args.command == "scan":
        print(json.dumps(await _scan_one(args, registry.get(args.id)), sort_keys=True))
        return

    results = []
    for source in registry.load():
        if not source.enabled:
            continue
        try:
            results.append(await _scan_one(args, source))
        except (FileNotFoundError, ValueError) as exc:
            results.append(
                {
                    "source_id": source.id,
                    "status": "ERROR",
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
    print(json.dumps(results, sort_keys=True))


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
