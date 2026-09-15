#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--challenger-queue", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--wide-enriched-dir", type=Path, required=True)
    parser.add_argument("--market-dir", type=Path, required=True)
    return parser


def main() -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("Lane 1 prospective review refuses REAL_TRADING_ENABLED=YES")
    args = build_parser().parse_args()
    script = Path(__file__).with_name("prospective_champion_lane.py")
    spec = importlib.util.spec_from_file_location("lane1_prospective_review_target", script)
    if spec is None or spec.loader is None:
        raise SystemExit(f"unable to load prospective evaluator: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    module.BASE = args.output_dir
    module.CFG = args.output_dir / "config.json"
    module.REPORT = args.output_dir / "report.json"
    module.WIDE = args.wide_enriched_dir
    module.MARKET = args.market_dir

    original_argv = sys.argv
    try:
        sys.argv = [
            str(script),
            "--challenger-queue",
            str(args.challenger_queue),
        ]
        module.main()
    finally:
        sys.argv = original_argv


if __name__ == "__main__":
    main()
