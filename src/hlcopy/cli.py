from __future__ import annotations

import argparse

from hlcopy.config import Settings
from hlcopy.pipeline import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hlcopy")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pipeline", help="discover, ingest, reconstruct, analyze and rank wallets")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "pipeline":
        run(Settings.from_env())


if __name__ == "__main__":
    main()
