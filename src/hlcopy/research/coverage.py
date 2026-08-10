from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hlcopy.config import Settings
from hlcopy.db.postgres import Database
from hlcopy.discovery.leaderboard import parse_leaderboard, shortlist
from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.models import Fill
from hlcopy.resolver.engine import ResolverConfig, _load_source_signals, _recent_signals
from hlcopy.resolver.matcher import evidence_events, select_anchor_trades
from hlcopy.resolver.source_registry import ExternalSourceSpec


@dataclass(frozen=True, slots=True)
class CoverageConfig:
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
class CoverageResult:
    source_id: str
    scanned_this_run: int
    scanned_total: int
    universe_size: int
    exhausted: bool
    state_path: str


def _state_path(output_dir: Path, source_id: str) -> Path:
    return output_dir / f"external_coverage_state_{source_id}.json"


def _load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"version": 1, "checked_addresses": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("coverage state must be a JSON object")
    checked = payload.get("checked_addresses", [])
    if not isinstance(checked, list):
        raise ValueError("coverage state checked_addresses must be a list")
    return payload


def _write_state(
    path: Path,
    *,
    source_id: str,
    checked_addresses: set[str],
    leaderboard_snapshot_ms: int,
    universe_addresses: tuple[str, ...],
    evidence_start_ms: int,
    evidence_end_ms: int,
) -> None:
    universe_payload = json.dumps(sorted(universe_addresses), separators=(",", ":")).encode()
    payload = {
        "version": 1,
        "source_id": source_id,
        "updated_at": datetime.now(tz=UTC).isoformat(),
        "leaderboard_snapshot_ms": leaderboard_snapshot_ms,
        "universe_size": len(universe_addresses),
        "universe_fingerprint": hashlib.sha256(universe_payload).hexdigest(),
        "evidence_start_ms": evidence_start_ms,
        "evidence_end_ms": evidence_end_ms,
        "checked_addresses": sorted(checked_addresses),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _elapsed(started: float) -> str:
    seconds = max(0, int(time.monotonic() - started))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:d}m{seconds:02d}s"


async def populate_external_evidence_coverage(
    *,
    source: ExternalSourceSpec,
    settings: Settings,
    output_dir: Path,
    resolver_config: ResolverConfig,
    coverage_config: CoverageConfig,
) -> CoverageResult:
    """Populate public Hyperliquid fills needed by the resolver; never score identity."""
    run_started = time.monotonic()
    all_signals = _load_source_signals(source)
    recent = _recent_signals(all_signals, resolver_config.evidence_lookback_days)
    anchors = select_anchor_trades(recent, max_trades=resolver_config.anchor_trades)
    events = evidence_events(anchors)
    if len(events) < 12:
        raise ValueError("insufficient external evidence for coverage crawl")

    start_ms = min(event.timestamp_ms for event in events) - resolver_config.time_tolerance_ms
    end_ms = max(event.timestamp_ms for event in events) + resolver_config.time_tolerance_ms
    state_path = _state_path(output_dir, source.id)
    state = _load_state(state_path)

    # A changed evidence window invalidates the old checkpoint. This prevents the
    # crawler from claiming coverage for addresses fetched against stale evidence.
    if (
        state.get("evidence_start_ms") not in {None, start_ms}
        or state.get("evidence_end_ms") not in {None, end_ms}
    ):
        checked_addresses: set[str] = set()
    else:
        checked_addresses = {
            str(address).lower()
            for address in state.get("checked_addresses", [])
            if str(address)
        }

    print(
        f"coverage progress source={source.id} stage=database_init status=started",
        flush=True,
    )
    async with Database(settings.database_url) as db:
        await db.init_schema()
        async with HyperliquidHttpClient(
            settings.api_url,
            settings.leaderboard_url,
            concurrency=settings.http_concurrency,
        ) as client:
            print(
                f"coverage progress source={source.id} stage=leaderboard_fetch status=started",
                flush=True,
            )
            leaderboard_response = await client.leaderboard()
            await db.store_raw(
                source="hyperliquid",
                endpoint=leaderboard_response.endpoint,
                request_payload=None,
                response_payload=leaderboard_response.response_payload,
                fetched_at_ms=leaderboard_response.fetched_at_ms,
            )
            candidates = parse_leaderboard(leaderboard_response.response_payload)
            print(
                f"coverage progress source={source.id} stage=leaderboard_fetch "
                f"status=complete wallets={len(candidates)} elapsed={_elapsed(run_started)}",
                flush=True,
            )

            upsert_started = time.monotonic()

            def leaderboard_progress(done: int, total: int) -> None:
                percent = (100.0 * done / total) if total else 100.0
                print(
                    f"coverage progress source={source.id} stage=leaderboard_db_upsert "
                    f"done={done}/{total} percent={percent:.1f} "
                    f"stage_elapsed={_elapsed(upsert_started)}",
                    flush=True,
                )

            await db.upsert_leaderboard(
                candidates,
                leaderboard_response.fetched_at_ms,
                progress=leaderboard_progress,
            )
            ordered = shortlist(
                candidates,
                limit=coverage_config.universe_limit,
                min_account_value=coverage_config.min_account_value,
                min_month_roi=coverage_config.min_month_roi,
                min_month_volume=coverage_config.min_month_volume,
            )
            universe_addresses = tuple(candidate.address.lower() for candidate in ordered)
            batch = [
                candidate
                for candidate in ordered
                if candidate.address.lower() not in checked_addresses
            ][: coverage_config.batch_size]
            print(
                f"coverage progress source={source.id} stage=wallet_fetch status=started "
                f"batch={len(batch)} checked={len(checked_addresses)} "
                f"universe={len(universe_addresses)}",
                flush=True,
            )

            wallet_started = time.monotonic()
            for index, candidate in enumerate(batch, start=1):
                print(
                    f"coverage [{index}/{len(batch)}] {candidate.address} "
                    f"cheap_score={candidate.cheap_score} elapsed={_elapsed(wallet_started)}",
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
        evidence_start_ms=start_ms,
        evidence_end_ms=end_ms,
    )
    scanned_total = len(set(universe_addresses) & checked_addresses)
    print(
        f"coverage progress source={source.id} stage=complete scanned_this_run={len(batch)} "
        f"scanned_total={scanned_total}/{len(universe_addresses)} "
        f"elapsed={_elapsed(run_started)}",
        flush=True,
    )
    return CoverageResult(
        source_id=source.id,
        scanned_this_run=len(batch),
        scanned_total=scanned_total,
        universe_size=len(universe_addresses),
        exhausted=scanned_total >= len(universe_addresses),
        state_path=str(state_path),
    )
