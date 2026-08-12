from __future__ import annotations

import argparse
import asyncio
import json
from decimal import Decimal
from pathlib import Path

from hlcopy.resolver.identifier import identify_wallet_from_csv
from hlcopy.resolver.public_trade_index import PublicTradeDiscoveryConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.resolver.identify_cli")
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/resolver"))
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--anchor-trades", type=int, default=8)
    parser.add_argument("--window-seconds", type=int, default=120)
    parser.add_argument("--max-price-bps", type=Decimal, default=Decimal("25"))
    parser.add_argument("--min-discovery-matches", type=int, default=3)
    parser.add_argument("--official-verify-trades", type=int, default=6)
    parser.add_argument("--official-time-tolerance-ms", type=int, default=12_000)
    parser.add_argument("--official-price-tolerance-bps", type=Decimal, default=Decimal("12"))
    parser.add_argument("--min-official-matches", type=int, default=3)
    parser.add_argument("--min-official-ratio", type=Decimal, default=Decimal("0.60"))
    return parser


async def _run(args: argparse.Namespace) -> None:
    config = PublicTradeDiscoveryConfig(
        anchor_trades=max(3, args.anchor_trades),
        window_seconds=max(1, args.window_seconds),
        max_price_bps=args.max_price_bps,
        min_discovery_matches=max(1, args.min_discovery_matches),
        official_verify_trades=max(1, args.official_verify_trades),
        official_time_tolerance_ms=max(1, args.official_time_tolerance_ms),
        official_price_tolerance_bps=args.official_price_tolerance_bps,
        min_official_matches=max(1, args.min_official_matches),
        min_official_ratio=args.min_official_ratio,
    )
    result = await identify_wallet_from_csv(
        args.evidence,
        output_dir=args.output_dir,
        cache_dir=args.cache_dir,
        config=config,
    )
    print(json.dumps(result.to_dict(), sort_keys=True))


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
