from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from hlcopy.copyability.slippage import BookLevel

D = Decimal


@dataclass(frozen=True, slots=True)
class L2Snapshot:
    coin: str
    timestamp_ms: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    @property
    def mid(self) -> Decimal:
        if not self.bids or not self.asks:
            raise ValueError("snapshot needs both bids and asks")
        return (self.bids[0].price + self.asks[0].price) / D("2")


def _unwrap_book(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("channel") == "l2Book" and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if "coin" in payload and "time" in payload and isinstance(payload.get("levels"), list):
        return payload
    return None


def parse_l2_snapshot(payload: Any) -> L2Snapshot | None:
    book = _unwrap_book(payload)
    if book is None:
        return None
    levels = book.get("levels") or []
    if len(levels) != 2:
        return None

    def parse_side(rows: Any, *, reverse: bool) -> tuple[BookLevel, ...]:
        parsed: list[BookLevel] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    price = D(str(row["px"]))
                    size = D(str(row["sz"]))
                except (KeyError, ValueError, InvalidOperation):
                    continue
                if price > 0 and size > 0:
                    parsed.append(BookLevel(price=price, size=size))
        parsed.sort(key=lambda level: level.price, reverse=reverse)
        return tuple(parsed)

    bids = parse_side(levels[0], reverse=True)
    asks = parse_side(levels[1], reverse=False)
    if not bids or not asks:
        return None
    return L2Snapshot(
        coin=str(book["coin"]).upper(),
        timestamp_ms=int(book["time"]),
        bids=bids,
        asks=asks,
    )


class LocalArchiveBookProvider:
    """Read decompressed Hyperliquid archive l2Book JSON-lines files.

    Expected layout mirrors the official archive:
      ROOT/YYYYMMDD/H/l2Book/COIN
    `.jsonl` and `.json` suffixes are also accepted.

    The provider returns the latest known exchange snapshot at-or-before the requested
    timestamp. It never selects a future snapshot, which avoids look-ahead.
    """

    def __init__(self, root: Path, *, max_book_age_ms: int = 1_000):
        self.root = root
        self.max_book_age_ms = max_book_age_ms
        self._cache: dict[tuple[str, str, int], tuple[list[int], list[L2Snapshot]]] = {}

    def _candidate_paths(self, coin: str, timestamp_ms: int) -> list[Path]:
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        base = self.root / dt.strftime("%Y%m%d") / str(dt.hour) / "l2Book"
        return [
            base / coin,
            base / f"{coin}.jsonl",
            base / f"{coin}.json",
        ]

    def _load_hour(self, coin: str, timestamp_ms: int) -> tuple[list[int], list[L2Snapshot]]:
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        key = (coin, dt.strftime("%Y%m%d"), dt.hour)
        if key in self._cache:
            return self._cache[key]

        path = next(
            (
                candidate
                for candidate in self._candidate_paths(coin, timestamp_ms)
                if candidate.exists()
            ),
            None,
        )
        if path is None:
            result: tuple[list[int], list[L2Snapshot]] = ([], [])
            self._cache[key] = result
            return result

        snapshots: list[L2Snapshot] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                snapshot = parse_l2_snapshot(payload)
                if snapshot is not None and snapshot.coin == coin:
                    snapshots.append(snapshot)
        snapshots.sort(key=lambda item: item.timestamp_ms)
        timestamps = [snapshot.timestamp_ms for snapshot in snapshots]
        result = (timestamps, snapshots)
        self._cache[key] = result
        return result

    def snapshot_at_or_before(self, coin: str, timestamp_ms: int) -> L2Snapshot | None:
        coin = coin.upper()
        candidates: list[L2Snapshot] = []
        for lookup_ms in (timestamp_ms, timestamp_ms - self.max_book_age_ms):
            timestamps, snapshots = self._load_hour(coin, lookup_ms)
            idx = bisect.bisect_right(timestamps, timestamp_ms) - 1
            if idx >= 0:
                candidates.append(snapshots[idx])
        if not candidates:
            return None
        snapshot = max(candidates, key=lambda item: item.timestamp_ms)
        if timestamp_ms - snapshot.timestamp_ms > self.max_book_age_ms:
            return None
        return snapshot
