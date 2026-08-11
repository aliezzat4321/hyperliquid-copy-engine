from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from hlcopy.market.symbols import canonical_coin

logger = logging.getLogger(__name__)


class UserFillsByTimeClient(Protocol):
    async def user_fills_by_time(
        self,
        user: str,
        start_time_ms: int,
        end_time_ms: int | None = None,
    ) -> list[Any]: ...


class JsonlOfficialFillSink:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = asyncio.Lock()

    async def put(self, row: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._append, row)

    def _append(self, row: dict[str, Any]) -> None:
        timestamp_ns = int(row.get("received_at_ns") or time.time_ns())
        day = time.strftime("%Y-%m-%d", time.gmtime(timestamp_ns / 1_000_000_000))
        path = self.root / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
            handle.flush()


def _payload_rows(pages: Iterable[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in pages:
        payload = getattr(page, "response_payload", None)
        if payload is None and isinstance(page, dict):
            payload = page.get("response_payload", page.get("data"))
        if not isinstance(payload, list):
            continue
        rows.extend(row for row in payload if isinstance(row, dict))
    return rows


def _match_fill(
    rows: Iterable[dict[str, Any]],
    *,
    tid: int,
    coin: str,
    exchange_ts_ms: int,
) -> dict[str, Any] | None:
    canonical = canonical_coin(coin)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        try:
            row_tid = int(row.get("tid"))
            row_ts = int(row.get("time"))
        except (TypeError, ValueError):
            continue
        if row_tid != tid or canonical_coin(row.get("coin", "")) != canonical:
            continue
        candidates.append(row)
    if not candidates:
        return None
    return min(candidates, key=lambda row: abs(int(row.get("time", 0)) - exchange_ts_ms))


def _load_checkpoint(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    offsets = payload.get("offsets", {}) if isinstance(payload, dict) else {}
    if not isinstance(offsets, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in offsets.items():
        try:
            result[str(key)] = max(0, int(value))
        except (TypeError, ValueError):
            continue
    return result


def _save_checkpoint(path: Path, offsets: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at_ns": time.time_ns(),
        "offsets": dict(sorted(offsets.items())),
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class WideTradeOfficialEnricher:
    """Resolve public wallet-trade hits to authoritative Hyperliquid user fills.

    The public ``trades`` stream scales to many locally-filtered wallets but does not
    expose ``dir`` or ``startPosition``. Once a tracked wallet is observed there, a
    tightly-bounded ``userFillsByTime`` request can recover the authoritative fill by
    exact trade id. Both timestamps are retained: public-stream receipt is the earliest
    scalable signal, while enrichment completion is the decision time available to a
    REST-confirmed implementation.
    """

    def __init__(
        self,
        *,
        source_dir: Path,
        checkpoint_path: Path,
        client: UserFillsByTimeClient,
        sink: JsonlOfficialFillSink,
        poll_seconds: float = 0.5,
        query_window_ms: int = 2_000,
        retry_delays: tuple[float, ...] = (0.0, 0.25, 1.0, 2.0),
        heartbeat_seconds: float = 60.0,
    ) -> None:
        self.source_dir = source_dir
        self.checkpoint_path = checkpoint_path
        self.client = client
        self.sink = sink
        self.poll_seconds = max(0.1, poll_seconds)
        self.query_window_ms = max(250, query_window_ms)
        self.retry_delays = retry_delays or (0.0,)
        self.heartbeat_seconds = max(5.0, heartbeat_seconds)
        self.offsets = _load_checkpoint(checkpoint_path)
        self.events_seen = 0
        self.matched = 0
        self.missed = 0
        self.last_event_exchange_ms: int | None = None

    async def run(self) -> None:
        next_heartbeat = time.monotonic() + self.heartbeat_seconds
        while True:
            processed = await self.drain_once()
            now = time.monotonic()
            if now >= next_heartbeat:
                logger.info(
                    "wide official enrichment heartbeat: events=%d matched=%d "
                    "missed=%d last_exchange_ms=%s",
                    self.events_seen,
                    self.matched,
                    self.missed,
                    self.last_event_exchange_ms,
                )
                next_heartbeat = now + self.heartbeat_seconds
            if processed == 0:
                await asyncio.sleep(self.poll_seconds)

    async def drain_once(self) -> int:
        processed = 0
        for path in sorted(self.source_dir.glob("*.jsonl")):
            processed += await self._drain_file(path)
        if processed:
            await asyncio.to_thread(_save_checkpoint, self.checkpoint_path, self.offsets)
        return processed

    async def _drain_file(self, path: Path) -> int:
        key = str(path.resolve())
        offset = self.offsets.get(key, 0)
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        if offset > size:
            offset = 0
        count = 0
        with path.open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            while True:
                start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    handle.seek(start)
                    break
                end = handle.tell()
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    self.offsets[key] = end
                    continue
                if isinstance(row, dict) and row.get("kind") == "public_wallet_trade":
                    await self.enrich(row)
                    count += 1
                self.offsets[key] = end
        return count

    async def enrich(self, event: dict[str, Any]) -> dict[str, Any]:
        self.events_seen += 1
        address = str(event.get("wallet_address") or "").lower()
        coin = canonical_coin(event.get("coin", ""))
        try:
            tid = int(event["tid"])
            exchange_ts_ms = int(event["exchange_ts_ms"])
            public_received_at_ns = int(event["received_at_ns"])
        except (KeyError, TypeError, ValueError) as exc:
            row = self._miss_row(event, reason=f"INVALID_PUBLIC_EVENT:{type(exc).__name__}")
            self.missed += 1
            await self.sink.put(row)
            return row

        self.last_event_exchange_ms = exchange_ts_ms
        match: dict[str, Any] | None = None
        last_error: str | None = None
        attempts = 0
        for delay in self.retry_delays:
            if delay > 0:
                await asyncio.sleep(delay)
            attempts += 1
            try:
                pages = await self.client.user_fills_by_time(
                    address,
                    exchange_ts_ms - self.query_window_ms,
                    exchange_ts_ms + self.query_window_ms,
                )
                match = _match_fill(
                    _payload_rows(pages),
                    tid=tid,
                    coin=coin,
                    exchange_ts_ms=exchange_ts_ms,
                )
            except Exception as exc:  # network/rate-limit errors are recorded, not hidden
                last_error = f"{type(exc).__name__}:{exc}"
                logger.warning(
                    "wide official fill enrichment failed wallet=%s tid=%s attempt=%d: %s",
                    address,
                    tid,
                    attempts,
                    last_error,
                )
                continue
            if match is not None:
                break

        completed_at_ns = time.time_ns()
        if match is None:
            self.missed += 1
            row = self._miss_row(
                event,
                reason="OFFICIAL_FILL_NOT_FOUND",
                attempts=attempts,
                completed_at_ns=completed_at_ns,
                last_error=last_error,
            )
            await self.sink.put(row)
            return row

        self.matched += 1
        decision_delay_ms = (completed_at_ns - public_received_at_ns) / 1_000_000
        exchange_to_decision_ms = completed_at_ns / 1_000_000 - exchange_ts_ms
        row = {
            "kind": "wide_official_fill",
            "wallet_id": event.get("wallet_id"),
            "wallet_label": event.get("wallet_label"),
            "wallet_stage": event.get("wallet_stage"),
            "wallet_address": address,
            "coin": coin,
            "tid": tid,
            "exchange_ts_ms": exchange_ts_ms,
            "public_received_at_ns": public_received_at_ns,
            "received_at_ns": completed_at_ns,
            "public_observed_lag_ms": event.get("observed_event_lag_ms"),
            "rest_confirmation_delay_ms": decision_delay_ms,
            "exchange_to_confirmed_decision_ms": exchange_to_decision_ms,
            "target_side": event.get("target_side"),
            "public_px": event.get("px"),
            "public_sz": event.get("sz"),
            "official_dir": match.get("dir"),
            "official_start_position": match.get("startPosition"),
            "official_oid": match.get("oid"),
            "official_crossed": match.get("crossed"),
            "official_fee": match.get("fee"),
            "official_fee_token": match.get("feeToken"),
            "official_fill": match,
            "attempts": attempts,
        }
        await self.sink.put(row)
        return row

    def _miss_row(
        self,
        event: dict[str, Any],
        *,
        reason: str,
        attempts: int = 0,
        completed_at_ns: int | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        completed = completed_at_ns or time.time_ns()
        public_received = event.get("received_at_ns")
        delay_ms = None
        try:
            delay_ms = (completed - int(public_received)) / 1_000_000
        except (TypeError, ValueError):
            pass
        return {
            "kind": "wide_official_fill_miss",
            "wallet_id": event.get("wallet_id"),
            "wallet_label": event.get("wallet_label"),
            "wallet_stage": event.get("wallet_stage"),
            "wallet_address": str(event.get("wallet_address") or "").lower(),
            "coin": canonical_coin(event.get("coin", "")),
            "tid": event.get("tid"),
            "exchange_ts_ms": event.get("exchange_ts_ms"),
            "public_received_at_ns": public_received,
            "received_at_ns": completed,
            "rest_confirmation_delay_ms": delay_ms,
            "reason": reason,
            "attempts": attempts,
            "last_error": last_error,
        }
