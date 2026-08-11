from __future__ import annotations

import argparse
import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.resolver.engine import ResolverConfig, resolve_source
from hlcopy.resolver.source_registry import ExternalSourceRegistry, ExternalSourceSpec
from hlcopy.shadow.registry import WalletRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.resolver.cli")
    parser.add_argument("--source-registry", required=True, type=Path)
    parser.add_argument("--wallet-registry", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")

    add = sub.add_parser("add")
    add.add_argument("--id", required=True)
    add.add_argument("--label", required=True)
    add.add_argument("--adapter", required=True, choices=["invo_closed_trades_csv"])
    add.add_argument("--evidence-path", required=True)
    add.add_argument("--notes", default="")

    resolve = sub.add_parser("resolve")
    resolve.add_argument("--id", required=True)
    _add_resolver_options(resolve)

    resolve_all = sub.add_parser("run-all")
    _add_resolver_options(resolve_all)
    return parser


def _add_resolver_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--anchor-trades", type=int, default=16)
    parser.add_argument("--evidence-lookback-days", type=int, default=14)
    parser.add_argument("--time-tolerance-ms", type=int, default=5_000)
    parser.add_argument("--price-tolerance-bps", type=Decimal, default=Decimal("5"))
    parser.add_argument("--max-candidates", type=int, default=5_000)
    parser.add_argument("--report-candidates", type=int, default=25)


def _config(args: argparse.Namespace) -> ResolverConfig:
    return ResolverConfig(
        anchor_trades=max(6, args.anchor_trades),
        evidence_lookback_days=max(1, args.evidence_lookback_days),
        time_tolerance_ms=max(1, args.time_tolerance_ms),
        price_tolerance_bps=args.price_tolerance_bps,
        max_candidates=max(1, args.max_candidates),
        report_candidates=max(1, args.report_candidates),
    )


async def _resolve_one(args: argparse.Namespace, source: ExternalSourceSpec) -> dict[str, object]:
    settings = Settings.from_env()
    result = await resolve_source(
        source=source,
        database_url=settings.database_url,
        wallet_registry=WalletRegistry(args.wallet_registry),
        output_dir=args.output_dir,
        config=_config(args),
    )
    return {
        "source_id": result.source_id,
        "status": result.status,
        "verified_address": result.verified_address,
        "evidence_trades": result.evidence_trades,
        "evidence_events": result.evidence_events,
        "candidate_wallets": result.candidate_wallets,
        "report_path": result.report_path,
    }


async def _run(args: argparse.Namespace) -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("external resolver refuses to run with REAL_TRADING_ENABLED=YES")
    source_registry = ExternalSourceRegistry(args.source_registry)
    wallet_registry = WalletRegistry(args.wallet_registry)

    if args.command == "init":
        source_registry.init()
        wallet_registry.init()
        print(args.source_registry)
        return

    if args.command == "add":
        source_registry.init()
        stored = source_registry.add(
            ExternalSourceSpec(
                id=args.id,
                label=args.label,
                adapter=args.adapter,
                evidence_path=args.evidence_path,
                notes=args.notes,
            )
        )
        print(json.dumps(stored.to_dict(), sort_keys=True))
        return

    if args.command == "resolve":
        print(json.dumps(await _resolve_one(args, source_registry.get(args.id)), sort_keys=True))
        return

    results = []
    for source in source_registry.load():
        if not source.enabled:
            continue
        try:
            results.append(await _resolve_one(args, source))
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
