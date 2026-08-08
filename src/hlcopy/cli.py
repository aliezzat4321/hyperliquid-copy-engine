from __future__ import annotations

import argparse
import asyncio
import logging

from hlcopy.config import Settings
from hlcopy.market.capture import capture_market
from hlcopy.pipeline import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hlcopy")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pipeline", help="discover, ingest, reconstruct, analyze and rank wallets")
    capture = sub.add_parser(
        "capture-market",
        help="continuously record Hyperliquid BBO, L2, trades and asset context",
    )
    capture.add_argument(
        "--coins",
        nargs="+",
        help="coin symbols to capture; defaults to HLCOPY_MARKET_COINS",
    )
    return parser


def _run_capture(settings: Settings, coins: list[str] | None) -> None:
    selected = tuple(coin.upper() for coin in coins) if coins else settings.market_coins
    print(
        f"capturing {','.join(selected)} from {settings.ws_url} into {settings.market_data_dir}",
        flush=True,
    )
    try:
        asyncio.run(
            capture_market(
                ws_url=settings.ws_url,
                coins=selected,
                output_dir=settings.market_data_dir,
                flush_rows=settings.market_flush_rows,
                flush_seconds=settings.market_flush_seconds,
                queue_size=settings.market_queue_size,
                heartbeat_seconds=settings.ws_heartbeat_seconds,
                reconnect_base_seconds=settings.ws_reconnect_base_seconds,
                reconnect_max_seconds=settings.ws_reconnect_max_seconds,
            )
        )
    except KeyboardInterrupt:
        print("market capture stopped", flush=True)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args()
    settings = Settings.from_env()
    if args.command == "pipeline":
        run(settings)
    elif args.command == "capture-market":
        _run_capture(settings, args.coins)


if __name__ == "__main__":
    main()
