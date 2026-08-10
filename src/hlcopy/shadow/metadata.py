from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.shadow.manifest import fingerprint


async def record_perp_metadata(
    *,
    client: HyperliquidHttpClient,
    output_dir: Path,
    interval_seconds: float = 21_600.0,
) -> None:
    """Persist point-in-time perp universe/margin tables; never backfill from future metadata."""
    output_dir.mkdir(parents=True, exist_ok=True)
    while True:
        response = await client.meta()
        payload = {
            "fetched_at_ms": response.fetched_at_ms,
            "response_payload": response.response_payload,
        }
        payload["fingerprint"] = fingerprint(payload)
        path = output_dir / f"meta_{response.fetched_at_ms}_{payload['fingerprint'][:12]}.json"
        if not path.exists():
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        await asyncio.sleep(max(300.0, interval_seconds))
