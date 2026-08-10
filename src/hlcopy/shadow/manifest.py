from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from hlcopy.shadow.registry import WalletRegistry


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ShadowRunManifest:
    run_id: str
    started_at: str
    registry_fingerprint: str
    registry_snapshot: tuple[dict[str, object], ...]
    extra_coins: tuple[str, ...]
    initial_market_coins: tuple[str, ...]
    websocket_url: str
    git_commit: str | None
    evidence_mode: str = "PROSPECTIVE_LIVE_SHADOW"
    source_latency_mode: str = "MEASURED_EXCHANGE_TO_LOCAL_RECEIPT"
    order_latency_mode: str = "NOT_YET_MEASURED_NO_ORDERS_SENT"
    real_trading_enabled: str = "NO"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def write_run_manifest(
    *,
    registry: WalletRegistry,
    shadow_dir: Path,
    websocket_url: str,
    extra_coins: tuple[str, ...],
    initial_market_coins: tuple[str, ...],
) -> Path:
    wallets = tuple(wallet.to_dict() for wallet in registry.load())
    registry_fingerprint = fingerprint(wallets)
    now = datetime.now(tz=UTC)
    stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
    run_id = f"shadow-{stamp}-{registry_fingerprint[:10]}"
    manifest = ShadowRunManifest(
        run_id=run_id,
        started_at=now.isoformat(),
        registry_fingerprint=registry_fingerprint,
        registry_snapshot=wallets,
        extra_coins=extra_coins,
        initial_market_coins=initial_market_coins,
        websocket_url=websocket_url,
        git_commit=os.getenv("HLCOPY_GIT_COMMIT"),
        real_trading_enabled=os.getenv("REAL_TRADING_ENABLED", "NO"),
    )
    path = shadow_dir / "manifests" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return path
