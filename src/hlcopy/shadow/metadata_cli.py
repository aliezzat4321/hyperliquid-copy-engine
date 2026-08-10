from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.shadow.manifest import fingerprint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.shadow.metadata_cli")
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


async def _snapshot(output_dir: Path) -> Path:
    settings = Settings.from_env()
    async with HyperliquidHttpClient(
        settings.api_url,
        settings.leaderboard_url,
        concurrency=1,
    ) as client:
        response = await client.meta()
    payload = {
        "fetched_at_ms": response.fetched_at_ms,
        "response_payload": response.response_payload,
    }
    payload["fingerprint"] = fingerprint(payload)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"meta_{response.fetched_at_ms}_{payload['fingerprint'][:12]}.json"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return path


def main() -> None:
    args = build_parser().parse_args()
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("metadata capture refuses to run with REAL_TRADING_ENABLED=YES")
    path = asyncio.run(_snapshot(args.output_dir))
    print(path, flush=True)


if __name__ == "__main__":
    main()
