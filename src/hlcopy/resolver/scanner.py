from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.db.postgres import Database
from hlcopy.discovery.leaderboard import parse_leaderboard, shortlist
from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.models import Fill
from hlcopy.resolver.engine import (
    ResolverConfig,
    ResolverRun,
    _load_source_signals,
    _recent_signals,
    resolve_source,
)
from hlcopy.resolver.matcher import evidence_events, select_anchor_trades
from hlcopy.resolver.source_registry import ExternalSourceSpec
from hlcopy.shadow.registry import WalletRegistry


@dataclass(frozen=True, slots=True)
class ScanConfig:
    batch_size: int = 50
    universe_limit: int = 5_000
    min_account_value: float = 0.0
    min_month_roi: float = 0.0
    min_month_volume: float = 0.0

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if self.universe_limit <= 0:
            raise ValueError("universe_limit must be positive")


@dataclass(frozen=True, slots=True)
class ScanResult:
    source_id: str
    scanned_this_run: int
    scanned_total: int
    universe_size: int
    exhausted: bool
    resolver: ResolverRun
    state_path: str


def _state_path(output_dir: Path, source_id: str) -> Path:
    return output_dir / f"external_scan_state_{source_id}.json"


def _load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"version": 1, "checked_addresses": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("resolver scan state must be a JSON object")
    checked = payload.get("checked_addresses", [])
    if not isinstance(checked, list):
        raise ValueError("resolver scan state checked_addresses must be a list")
    return payload


def _write_state(
    path: Path,
    *,
    source_id: str,
    checked_addresses: set[str],
    leaderboard_snapshot_ms: int,
    universe_addresses: tuple[str, ...],
) -> None:
    universe_payload = json.dumps(sorted(universe_addresses), separators=(",", ":")).encode()
    payload = {
        "version": 1,
        "source_id": source_id,
        "updated_at": datetime.now(tz=UTC).isoformat(),
        "leaderboard_snapshot_ms": leaderboard_snapshot_ms,
        "universe_size": len(universe_addresses),
        "universe_fingerprint": hashlib.sha256(universe_payload).hexdigest(),
        "checked_addresses": sorted(checked_addresses),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


async def scan_and_resolve(
    *,
    source: ExternalSourceSpec,
    settings: Settings,
    wallet_registry: WalletRegistry,
    output_dir: Path,
    resolver_config: ResolverConfig,
    scan_config: ScanConfig,
) -> ScanResult:
    all_signals = _load_source_signals(source)
    recent = _recent_signals(all_signals, resolver_config.evidence_lookback_days)
    anchors = select_anchor_trades(recent, max_trades=resolver_config.anchor_trades)
    events = evidence_events(anchors)
    if len(events) < 12:
        raise ValueError("insufficient external evidence for expanding scan")
    start_ms = min(event.timestamp_ms for event in events) - resolver_config.time_tolerance_ms
    end_ms = max(event.timestamp_ms for event in events) + resolver_config.time_tolerance_ms

    state_path = _state_path(output_dir, source.id)
    state = _load_state(state_path)
    checked_addresses = {
        str(address).lower() for address in state.get("checked_addresses", []) if str(address)
    }

    async with Database(settings.database_url) as db:
        await db.init_schema()
        async with HyperliquidHttpClient(
            settings.api_url,
            settings.leaderboard_url,
            concurrency=settings.http_concurrency,
        ) as client:
            leaderboard_response = await client.leaderboard()
            candidates = parse_leaderboard(leaderboard_response.response_payload)
            await db.upsert_leaderboard(candidates, leaderboard_response.fetched_at_ms)
            ordered = shortlist(
                candidates,
                limit=scan_config.universe_limit,
                min_account_value=scan_config.min_account_value,
                min_month_roi=scan_config.min_month_roi,
                min_month_volume=scan_config.min_month_volume,
            )
            universe_addresses = tuple(candidate.address for candidate in ordered)
            batch = [
                candidate
                for candidate in ordered
                if candidate.address.lower() not in checked_addresses
            ][: scan_config.batch_size]

            for index, candidate in enumerate(batch, start=1):
                print(
                    f"resolver scan [{index}/{len(batch)}] {candidate.address} "
                    f"cheap_score={candidate.cheap_score}",
                    flush=True,
                )
                pages = await client.user_fills_by_time(candidate.address, start_ms, end_ms)
                fills: list[Fill] = []
                for page in pages:
                    await db.store_raw(
                        source="hyperliquid",
                        endpoint="userFillsByTime",
                        request_payload=page.request_payload,
                        response_payload=page.response_payload,
                        fetched_at_ms=page.fetched_at_ms,
                    )
                    rows = page.response_payload if isinstance(page.response_payload, list) else []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        direction = str(row.get("dir", ""))
                        if "Long" not in direction and "Short" not in direction:
                            continue
                        try:
                            fills.append(Fill.from_raw(candidate.address, row))
                        except (KeyError, TypeError, ValueError, ArithmeticError):
                            continue
                if fills:
                    unique = {(fill.wallet_address, fill.tid): fill for fill in fills}
                    await db.upsert_fills(list(unique.values()))
                checked_addresses.add(candidate.address.lower())

    _write_state(
        state_path,
        source_id=source.id,
        checked_addresses=checked_addresses,
        leaderboard_snapshot_ms=leaderboard_response.fetched_at_ms,
        universe_addresses=universe_addresses,
    )

    resolver = await resolve_source(
        source=source,
        database_url=settings.database_url,
        wallet_registry=wallet_registry,
        output_dir=output_dir,
        config=resolver_config,
    )
    scanned_in_universe = len(set(universe_addresses) & checked_addresses)
    exhausted = scanned_in_universe >= len(universe_addresses)
    return ScanResult(
        source_id=source.id,
        scanned_this_run=len(batch),
        scanned_total=scanned_in_universe,
        universe_size=len(universe_addresses),
        exhausted=exhausted,
        resolver=resolver,
        state_path=str(state_path),
    )
