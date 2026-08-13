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
    parser.add_argument("--anchor-trades", type=int, default=8)
    parser.add_argument("--window-seconds", type=int, default=30)
    parser.add_argument("--max-price-bps", type=Decimal, default=Decimal("25"))
    parser.add_argument("--max-size-ratio-error", type=Decimal, default=Decimal("0.60"))
    parser.add_argument("--min-discovery-matches", type=int, default=3)
    parser.add_argument("--max-candidates-to-verify", type=int, default=6)
    parser.add_argument("--historical-verify-trades", type=int, default=12)
    parser.add_argument("--historical-lookback-hours", type=int, default=6)
    parser.add_argument("--historical-time-tolerance-ms", type=int, default=25_000)
    parser.add_argument(
        "--historical-price-tolerance-bps", type=Decimal, default=Decimal("35")
    )
    parser.add_argument(
        "--historical-entry-price-tolerance-bps", type=Decimal, default=Decimal("15")
    )
    parser.add_argument(
        "--historical-max-size-ratio-error", type=Decimal, default=Decimal("0.45")
    )
    parser.add_argument("--min-historical-matches", type=int, default=3)
    parser.add_argument("--min-historical-ratio", type=Decimal, default=Decimal("0.20"))
    parser.add_argument("--min-historical-winner-match-gap", type=int, default=2)
    return parser


async def _run(args: argparse.Namespace) -> None:
    config = PublicTradeDiscoveryConfig(
        anchor_trades=max(3, args.anchor_trades),
        window_seconds=max(1, args.window_seconds),
        max_price_bps=args.max_price_bps,
        max_size_ratio_error=args.max_size_ratio_error,
        min_discovery_matches=max(1, args.min_discovery_matches),
        max_candidates_to_verify=max(1, args.max_candidates_to_verify),
        historical_verify_trades=max(1, args.historical_verify_trades),
        historical_lookback_hours=max(1, args.historical_lookback_hours),
        historical_time_tolerance_ms=max(1, args.historical_time_tolerance_ms),
        historical_price_tolerance_bps=args.historical_price_tolerance_bps,
        historical_entry_price_tolerance_bps=(
            args.historical_entry_price_tolerance_bps
        ),
        historical_max_size_ratio_error=args.historical_max_size_ratio_error,
        min_historical_matches=max(1, args.min_historical_matches),
        min_historical_ratio=args.min_historical_ratio,
        min_historical_winner_match_gap=max(1, args.min_historical_winner_match_gap),
    )
    result = await identify_wallet_from_csv(
        args.evidence,
        output_dir=args.output_dir,
        config=config,
    )
    print(json.dumps(result.to_dict(), sort_keys=True))


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
