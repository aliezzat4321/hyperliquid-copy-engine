from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.shadow.wide_enrich import JsonlOfficialFillSink, WideTradeOfficialEnricher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.shadow.wide_enrich_cli")
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--query-window-ms", type=int, default=2_000)
    return parser


async def _run(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    sink = JsonlOfficialFillSink(args.output_dir)
    async with HyperliquidHttpClient(
        settings.api_url,
        settings.leaderboard_url,
        concurrency=settings.http_concurrency,
    ) as client:
        enricher = WideTradeOfficialEnricher(
            source_dir=args.source_dir,
            checkpoint_path=args.checkpoint,
            client=client,
            sink=sink,
            poll_seconds=args.poll_seconds,
            query_window_ms=args.query_window_ms,
        )
        await enricher.run()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit(
            "wide official fill enricher refuses to run with REAL_TRADING_ENABLED=YES"
        )
    args = build_parser().parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("wide official fill enricher stopped", flush=True)


if __name__ == "__main__":
    main()
