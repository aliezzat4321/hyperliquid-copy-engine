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
    parser.add_argument(
        "--all-dexes",
        action="store_true",
        help="discover perpDexs and snapshot default plus every HIP-3 DEX",
    )
    return parser


def _dex_names(payload: object) -> tuple[str, ...]:
    names = [""]
    if isinstance(payload, list):
        for row in payload:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if name and name not in names:
                names.append(name)
    return tuple(names)


async def _run(output: Path, dex: str, all_dexes: bool = False) -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("margin metadata collector refuses REAL_TRADING_ENABLED=YES")
    normalized_dex = dex.strip()
    if normalized_dex and all_dexes:
        raise SystemExit("--dex and --all-dexes are mutually exclusive")

    settings = Settings.from_env()
    records: list[dict[str, object]] = []
    async with HyperliquidHttpClient(
        settings.api_url,
        settings.leaderboard_url,
        concurrency=settings.http_concurrency,
    ) as client:
        if all_dexes:
            dex_response = await client.info({"type": "perpDexs"})
            dexes = _dex_names(dex_response.response_payload)
        else:
            dexes = (normalized_dex,)

        for name in dexes:
            request = {"type": "meta"} if not name else {"type": "meta", "dex": name}
            response = await client.info(request)
            fetched_at_ns = time.time_ns()
            parsed = parse_margin_metadata(
                response.response_payload,
                fetched_at_ns=fetched_at_ns,
                dex=name,
            )
            # Some builder DEXs may currently expose no active parseable table. They
            # are not useful for validation and must not be persisted as valid truth.
            if not parsed.tables:
                continue
            if response.request_payload != request:
                raise SystemExit("unexpected meta request provenance; refusing persistence")
            records.append(
                {
                    "fetched_at_ns": fetched_at_ns,
                    "fetched_at_ms": response.fetched_at_ms,
                    "network": settings.network,
                    "dex": name,
                    "request_payload": response.request_payload,
                    "payload": response.response_payload,
                }
            )

    if not records:
        raise SystemExit("official meta responses contained no parseable margin tables")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    dex_summary = ",".join(str(record["dex"]) or "default" for record in records)
    table_count = 0
    for record in records:
        parsed = parse_margin_metadata(
            record["payload"],
            fetched_at_ns=int(record["fetched_at_ns"]),
            dex=str(record["dex"]),
        )
        table_count += len(parsed.tables)
    print(
        "margin_snapshot "
        f"network={settings.network} dexes={len(records)} tables={table_count} "
        f"names={dex_summary} output={output}"
    )


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_run(args.output, args.dex, args.all_dexes))


if __name__ == "__main__":
    main()
