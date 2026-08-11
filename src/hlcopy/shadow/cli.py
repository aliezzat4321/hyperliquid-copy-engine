from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.market.symbols import canonical_coin
from hlcopy.shadow.capture import (
    load_market_coin_file,
    required_market_coins,
    run_shadow_validation,
)
from hlcopy.shadow.manifest import write_run_manifest
from hlcopy.shadow.registry import SOURCE_TYPES, STAGES, WalletRegistry, WalletSpec


def _registry(args: argparse.Namespace) -> WalletRegistry:
    return WalletRegistry(args.registry)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.shadow.cli")
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("data/shadow/wallets.json"),
        help="JSON wallet registry",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create an empty wallet registry if missing")
    sub.add_parser("list", help="list registered sources")

    add = sub.add_parser("add", help="add a research/validation source")
    add.add_argument("--id", required=True)
    add.add_argument("--label", required=True)
    add.add_argument("--source-type", choices=sorted(SOURCE_TYPES), required=True)
    add.add_argument("--source-ref", required=True)
    add.add_argument("--stage", choices=sorted(STAGES), default="research")
    add.add_argument("--coins", nargs="*", default=[])
    add.add_argument("--notes", default="")

    stage = sub.add_parser("stage", help="change a source lifecycle stage")
    stage.add_argument("id")
    stage.add_argument("stage", choices=sorted(STAGES))

    coins = sub.add_parser("coins", help="replace market-coverage coins for a source")
    coins.add_argument("id")
    coins.add_argument("coins", nargs="+")

    enable = sub.add_parser("enable", help="enable a source")
    enable.add_argument("id")
    disable = sub.add_parser("disable", help="disable a source")
    disable.add_argument("id")
    remove = sub.add_parser("remove", help="remove a source from the registry")
    remove.add_argument("id")

    run = sub.add_parser("run", help="run zero-cost prospective market + wallet shadow capture")
    run.add_argument("--shadow-dir", type=Path, required=True)
    run.add_argument("--market-dir", type=Path, required=True)
    run.add_argument(
        "--coins",
        nargs="*",
        default=[],
        help="extra market coins to capture even if no validation wallet declares them",
    )
    run.add_argument(
        "--coins-file",
        type=Path,
        default=None,
        help=(
            "newline-delimited market universe to merge into market capture; "
            "the file is re-read prospectively so cohort refreshes take effect"
        ),
    )
    return parser


def _print_wallet(wallet: WalletSpec) -> None:
    print(json.dumps(wallet.to_dict(), sort_keys=True))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args()
    registry = _registry(args)

    if args.command == "init":
        registry.init()
        print(registry.path)
        return
    if args.command == "list":
        for wallet in registry.load():
            _print_wallet(wallet)
        return
    if args.command == "add":
        wallet = registry.add(
            WalletSpec(
                id=args.id,
                label=args.label,
                source_type=args.source_type,
                source_ref=args.source_ref,
                stage=args.stage,
                coins=tuple(args.coins),
                notes=args.notes,
            )
        )
        _print_wallet(wallet)
        return
    if args.command == "stage":
        _print_wallet(registry.update(args.id, stage=args.stage))
        return
    if args.command == "coins":
        _print_wallet(registry.update(args.id, coins=tuple(args.coins)))
        return
    if args.command == "enable":
        _print_wallet(registry.update(args.id, enabled=True))
        return
    if args.command == "disable":
        _print_wallet(registry.update(args.id, enabled=False))
        return
    if args.command == "remove":
        registry.remove(args.id)
        print(f"removed {args.id}")
        return
    if args.command == "run":
        if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
            raise SystemExit("shadow validation refuses to run with REAL_TRADING_ENABLED=YES")
        settings = Settings.from_env()
        registry.init()
        extra_coins = tuple(
            normalized
            for coin in args.coins
            if (normalized := canonical_coin(coin))
        )
        file_coins = load_market_coin_file(args.coins_file)
        initial_extra_coins = tuple(dict.fromkeys((*extra_coins, *file_coins)))
        initial_market_coins = required_market_coins(registry, initial_extra_coins)
        manifest_path = write_run_manifest(
            registry=registry,
            shadow_dir=args.shadow_dir,
            websocket_url=settings.ws_url,
            extra_coins=initial_extra_coins,
            initial_market_coins=initial_market_coins,
        )
        print(
            "shadow validation starting; REAL TRADING IS NOT PART OF THIS PROCESS; "
            f"registry={registry.path} coins={','.join(initial_market_coins) or '-'} "
            f"manifest={manifest_path}",
            flush=True,
        )
        try:
            asyncio.run(
                run_shadow_validation(
                    ws_url=settings.ws_url,
                    registry=registry,
                    shadow_dir=args.shadow_dir,
                    market_dir=args.market_dir,
                    extra_coins=extra_coins,
                    extra_coins_file=args.coins_file,
                    market_flush_rows=settings.market_flush_rows,
                    market_flush_seconds=settings.market_flush_seconds,
                    market_queue_size=settings.market_queue_size,
                    heartbeat_seconds=settings.ws_heartbeat_seconds,
                    reconnect_base_seconds=settings.ws_reconnect_base_seconds,
                    reconnect_max_seconds=settings.ws_reconnect_max_seconds,
                )
            )
        except KeyboardInterrupt:
            print("shadow validation stopped", flush=True)
        return
    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    main()
