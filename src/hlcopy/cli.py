from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.copyability.archive_plan import required_l2_objects, write_fetch_script
from hlcopy.copyability.runner import run_matrix
from hlcopy.market.capture import capture_market
from hlcopy.pipeline import run
from hlcopy.profiling import run as run_profiles
from hlcopy.signals.invo import load_invo_closed_trades


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value cannot be negative")
    return parsed


def _positive_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("value must be numeric") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _since_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        if "T" in value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise SystemExit("--since must be YYYY-MM-DD or ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hlcopy")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pipeline", help="discover, ingest, reconstruct, analyze and rank wallets")
    profile = sub.add_parser(
        "profile-traders",
        help="build forensic profiles for top official Hyperliquid leaderboard traders",
    )
    profile.add_argument(
        "--limit",
        type=_positive_int,
        help="number of official leaderboard traders to profile",
    )
    profile.add_argument(
        "--lookback-days",
        type=_positive_int,
        help="historical fill and funding lookback in days",
    )
    capture = sub.add_parser(
        "capture-market",
        help="continuously record Hyperliquid BBO, L2, trades and asset context",
    )
    capture.add_argument(
        "--coins",
        nargs="+",
        help="coin symbols to capture; defaults to HLCOPY_MARKET_COINS",
    )

    backtest = sub.add_parser(
        "backtest-signals",
        help=(
            "replay normalized external trader signals with follower sizing "
            "and optional historical L2"
        ),
    )
    backtest.add_argument("--csv", required=True, type=Path, help="Invo closed-trade CSV export")
    backtest.add_argument("--coins", nargs="+", help="optional ticker filter, e.g. BTC ETH")
    backtest.add_argument(
        "--directions",
        nargs="+",
        choices=["LONG", "SHORT"],
        help="optional direction filter",
    )
    backtest.add_argument(
        "--since",
        help="only signals opened on/after YYYY-MM-DD or an ISO-8601 timestamp",
    )
    backtest.add_argument("--capital", type=_positive_decimal, default=Decimal("10000"))
    backtest.add_argument(
        "--latencies-ms",
        nargs="+",
        type=_nonnegative_int,
        default=[250, 500, 1000, 2000, 5000, 10000, 30000],
    )
    backtest.add_argument(
        "--leverage-grid",
        nargs="+",
        type=_positive_decimal,
        default=[Decimal("2"), Decimal("5"), Decimal("10")],
    )
    backtest.add_argument(
        "--taker-fee-bps",
        type=_positive_decimal,
        default=Decimal("4.5"),
        help="follower taker fee per side in bps; base Hyperliquid perp rate is 4.5 bps",
    )
    backtest.add_argument(
        "--max-slippage-bps",
        type=_positive_decimal,
        default=Decimal("20"),
    )
    backtest.add_argument(
        "--max-margin-pct",
        type=_positive_decimal,
        default=Decimal("5"),
        help="cap follower margin allocated to any one copied trade",
    )
    backtest.add_argument(
        "--max-total-margin-pct",
        type=_positive_decimal,
        default=Decimal("50"),
        help="cap aggregate concurrent follower margin reservation",
    )
    backtest.add_argument(
        "--archive-dir",
        type=Path,
        help="decompressed Hyperliquid archive root; omit for source-price baseline only",
    )
    backtest.add_argument(
        "--max-book-age-ms",
        type=_positive_int,
        default=1000,
        help="maximum age of a historical L2 snapshot used for execution",
    )

    plan = sub.add_parser(
        "plan-archive",
        help="write a requester-pays AWS/lz4 fetch script for L2 hours required by signal replay",
    )
    plan.add_argument("--csv", required=True, type=Path)
    plan.add_argument("--coins", nargs="+")
    plan.add_argument("--directions", nargs="+", choices=["LONG", "SHORT"])
    plan.add_argument("--since")
    plan.add_argument(
        "--latencies-ms",
        nargs="+",
        type=_nonnegative_int,
        default=[250, 500, 1000, 2000, 5000, 10000, 30000],
    )
    plan.add_argument(
        "--archive-dir",
        required=True,
        type=Path,
        help="destination root for decompressed official archive files",
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


def _load_signals(args: argparse.Namespace):
    result = load_invo_closed_trades(
        args.csv,
        coins=set(args.coins) if args.coins else None,
        directions=set(args.directions) if args.directions else None,
        since_ms=_since_ms(args.since),
    )
    print(
        f"loaded {len(result.signals)} signals from {args.csv}; "
        f"rejected malformed rows={len(result.rejected_rows)}",
        flush=True,
    )
    if not result.signals:
        raise SystemExit("no signals matched the requested filters")
    return result


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args()
    settings = Settings.from_env()
    if args.command == "pipeline":
        run(settings)
    elif args.command == "profile-traders":
        run_profiles(
            settings,
            limit=args.limit,
            lookback_days=args.lookback_days,
        )
    elif args.command == "capture-market":
        _run_capture(settings, args.coins)
    elif args.command == "backtest-signals":
        imported = _load_signals(args)
        if args.archive_dir is None:
            print(
                "WARNING: SOURCE_PRICE_BASELINE uses the source entry/exit prices and "
                "does not prove latency/slippage copyability.",
                flush=True,
            )
        matrix_json, matrix_csv = run_matrix(
            imported.signals,
            output_dir=settings.output_dir,
            capital=args.capital,
            latencies_ms=args.latencies_ms,
            leverages=args.leverage_grid,
            taker_fee_bps=args.taker_fee_bps,
            max_slippage_bps=args.max_slippage_bps,
            max_margin_fraction_per_trade=args.max_margin_pct / Decimal("100"),
            max_total_margin_fraction=args.max_total_margin_pct / Decimal("100"),
            archive_dir=args.archive_dir,
            max_book_age_ms=args.max_book_age_ms,
        )
        print(f"wrote {matrix_json}", flush=True)
        print(f"wrote {matrix_csv}", flush=True)
    elif args.command == "plan-archive":
        imported = _load_signals(args)
        objects = required_l2_objects(
            imported.signals,
            latencies_ms=args.latencies_ms,
        )
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        script_path = settings.output_dir / f"fetch_hl_archive_{stamp}.sh"
        write_fetch_script(objects, root=args.archive_dir, path=script_path)
        existing = sum(obj.local_path(args.archive_dir).exists() for obj in objects)
        print(
            f"required archive objects={len(objects)} already present={existing} "
            f"missing={len(objects)-existing}",
            flush=True,
        )
        print(
            "Requester-pays transfer is NOT executed automatically. "
            f"Review then run {script_path}",
            flush=True,
        )


if __name__ == "__main__":
    main()
