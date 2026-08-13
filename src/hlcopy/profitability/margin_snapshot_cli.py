from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.profitability.margin_tables import parse_margin_metadata


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hlcopy.profitability.margin_snapshot_cli"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/research/margin_metadata.jsonl"),
    )
    parser.add_argument("--dex", default="")
    return parser


async def _run(output: Path, dex: str) -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("margin metadata collector refuses REAL_TRADING_ENABLED=YES")

    # HyperliquidHttpClient.meta() currently issues exactly {"type": "meta"}, which
    # is default-perp metadata. Until the client has a tested DEX-aware metadata
    # request, accepting a non-empty --dex would persist default metadata under
    # false DEX provenance. Fail before creating a client or making any request.
    normalized_dex = dex.strip()
    if normalized_dex:
        raise SystemExit(
            "non-default --dex is unsupported: collector only fetches default-perp meta"
        )

    settings = Settings.from_env()
    async with HyperliquidHttpClient(
        settings.api_url,
        settings.leaderboard_url,
        concurrency=settings.http_concurrency,
    ) as client:
        response = await client.meta()

    fetched_at_ns = time.time_ns()
    # Validate before persistence. A malformed/changed upstream schema must fail closed.
    parsed = parse_margin_metadata(
        response.response_payload,
        fetched_at_ns=fetched_at_ns,
        dex="",
    )
    if not parsed.margin_tables:
        raise SystemExit("official meta response contained no parseable margin tables")

    # Persist the exact request provenance so downstream research can distinguish
    # default-perp metadata from any future DEX-aware transport unambiguously.
    if response.request_payload != {"type": "meta"}:
        raise SystemExit("unexpected meta request provenance; refusing snapshot persistence")

    record = {
        "fetched_at_ns": fetched_at_ns,
        "fetched_at_ms": response.fetched_at_ms,
        "network": settings.network,
        "dex": "",
        "request_payload": response.request_payload,
        "payload": response.response_payload,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    print(
        "margin_snapshot "
        f"network={settings.network} tables={len(parsed.margin_tables)} "
        f"fetched_at_ns={fetched_at_ns} output={output}"
    )


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_run(args.output, args.dex))


if __name__ == "__main__":
    main()
