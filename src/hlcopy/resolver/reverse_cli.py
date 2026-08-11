from __future__ import annotations

import argparse
import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.resolver.reverse_index import (
    HypeDexerCompletedTradesClient,
    ReverseResolverConfig,
    resolve_source_reverse_index,
)
from hlcopy.resolver.source_registry import ExternalSourceRegistry, ExternalSourceSpec
from hlcopy.shadow.registry import WalletRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.resolver.reverse_cli")
    parser.add_argument("--source-registry", required=True, type=Path)
    parser.add_argument("--wallet-registry", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--hypedexer-base-url", default="https://api.hypedexer.com")
    parser.add_argument("--anchor-trades", type=int, default=8)
    parser.add_argument("--primary-window-ms", type=int, default=120_000)
    parser.add_argument("--fallback-window-ms", type=int, default=600_000)
    parser.add_argument("--max-index-rows-per-anchor", type=int, default=5_000)
    parser.add_argument("--index-page-size", type=int, default=1_000)
    parser.add_argument("--max-index-price-bps", type=Decimal, default=Decimal("25"))
    parser.add_argument("--min-discovery-matches", type=int, default=3)
    parser.add_argument("--official-verify-trades", type=int, default=6)
    parser.add_argument("--official-time-tolerance-ms", type=int, default=12_000)
    parser.add_argument(
        "--official-price-tolerance-bps", type=Decimal, default=Decimal("12")
    )
    parser.add_argument("--min-official-matches", type=int, default=3)
    parser.add_argument("--min-official-ratio", type=Decimal, default=Decimal("0.60"))
    sub = parser.add_subparsers(dest="command", required=True)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--id", required=True)
    sub.add_parser("run-all")
    return parser


def _config(args: argparse.Namespace) -> ReverseResolverConfig:
    return ReverseResolverConfig(
        anchor_trades=max(3, args.anchor_trades),
        primary_window_ms=max(1_000, args.primary_window_ms),
        fallback_window_ms=max(args.primary_window_ms, args.fallback_window_ms),
        max_index_rows_per_anchor=max(100, args.max_index_rows_per_anchor),
        index_page_size=max(1, min(1_000, args.index_page_size)),
        max_index_price_bps=max(Decimal("0.1"), args.max_index_price_bps),
        min_discovery_matches=max(2, args.min_discovery_matches),
        official_verify_trades=max(3, args.official_verify_trades),
        official_time_tolerance_ms=max(1_000, args.official_time_tolerance_ms),
        official_price_tolerance_bps=max(
            Decimal("0.1"), args.official_price_tolerance_bps
        ),
        min_official_matches=max(2, args.min_official_matches),
        min_official_ratio=max(Decimal("0"), min(Decimal("1"), args.min_official_ratio)),
    )


async def _resolve_one(
    args: argparse.Namespace,
    source: ExternalSourceSpec,
    *,
    index_client: HypeDexerCompletedTradesClient,
    official_client: HyperliquidHttpClient,
) -> dict[str, object]:
    result = await resolve_source_reverse_index(
        source=source,
        index_client=index_client,
        official_client=official_client,
        wallet_registry=WalletRegistry(args.wallet_registry),
        output_dir=args.output_dir,
        config=_config(args),
        progress=lambda text: print(text, flush=True),
    )
    return {
        "source_id": result.source_id,
        "status": result.status,
        "address": result.address,
        "discovery_matches": result.discovery_matches,
        "official_matches": result.official_matches,
        "report_path": result.report_path,
    }


async def _run(args: argparse.Namespace) -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("reverse external resolver refuses REAL_TRADING_ENABLED=YES")
    api_key = os.getenv("HYPEDEXER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "HYPEDEXER_API_KEY is required; keep it on the VM and never commit it"
        )

    sources = ExternalSourceRegistry(args.source_registry)
    settings = Settings.from_env()
    selected: tuple[ExternalSourceSpec, ...]
    if args.command == "resolve":
        selected = (sources.get(args.id),)
    else:
        selected = tuple(source for source in sources.load() if source.enabled)

    results: list[dict[str, object]] = []
    async with HypeDexerCompletedTradesClient(
        api_key,
        base_url=args.hypedexer_base_url,
    ) as index_client:
        async with HyperliquidHttpClient(
            settings.api_url,
            settings.leaderboard_url,
            concurrency=settings.http_concurrency,
        ) as official_client:
            for index, source in enumerate(selected, start=1):
                print(
                    f"external reverse resolver source {index}/{len(selected)} "
                    f"id={source.id} label={source.label}",
                    flush=True,
                )
                try:
                    results.append(
                        await _resolve_one(
                            args,
                            source,
                            index_client=index_client,
                            official_client=official_client,
                        )
                    )
                except (FileNotFoundError, ValueError) as exc:
                    results.append(
                        {
                            "source_id": source.id,
                            "status": "ERROR",
                            "error": f"{type(exc).__name__}:{exc}",
                        }
                    )
    print(json.dumps(results, sort_keys=True), flush=True)


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
