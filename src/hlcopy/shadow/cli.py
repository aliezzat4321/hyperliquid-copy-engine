from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.shadow.capture import run_shadow_validation
from hlcopy.shadow.registry import STAGES, SOURCE_TYPES, WalletRegistry, WalletSpec


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
        settings = Settings.from_env()
        extra_coins = tuple(str(coin).upper() for coin in args.coins)
        print(
            "shadow validation starting; REAL TRADING IS NOT PART OF THIS PROCESS; "
            f"registry={registry.path} extra_coins={','.join(extra_coins) or '-'}",
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
