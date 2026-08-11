from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.shadow.registry import WalletRegistry
from hlcopy.shadow.wide_live import ProspectiveWideTradeCollector
from hlcopy.shadow.wide_watch import JsonlWideTradeSink


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.shadow.wide_cli_live")
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--coins-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-live-lag-ms", type=float, default=2_000.0)
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("wide public trade watcher refuses to run with REAL_TRADING_ENABLED=YES")

    args = build_parser().parse_args()
    settings = Settings.from_env()
    registry = WalletRegistry(args.registry)
    registry.init()
    sink = JsonlWideTradeSink(args.output_dir)
    collector = ProspectiveWideTradeCollector(
        ws_url=settings.ws_url,
        registry=registry,
        coins_file=args.coins_file,
        sink=sink,
        max_live_lag_ms=args.max_live_lag_ms,
        heartbeat_seconds=settings.ws_heartbeat_seconds,
        reconnect_base_seconds=settings.ws_reconnect_base_seconds,
        reconnect_max_seconds=settings.ws_reconnect_max_seconds,
    )
    try:
        asyncio.run(collector.run())
    except KeyboardInterrupt:
        print("wide public trade watcher stopped", flush=True)


if __name__ == "__main__":
    main()
