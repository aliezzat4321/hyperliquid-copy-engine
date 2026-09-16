from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from hlcopy.market.symbols import canonical_coin
from hlcopy.profitability.position_copy import CopyFillEvent, _event_from_fill


TargetKey = tuple[str, str]


def _normalized_target_keys(target_keys: set[TargetKey] | None) -> set[TargetKey] | None:
    if target_keys is None:
        return None
    return {(wallet.lower(), canonical_coin(coin)) for wallet, coin in target_keys}


def iter_wide_events_memory_bounded(
    enriched_dir: Path,
    *,
    cutoff_ns: int,
    target_keys: set[TargetKey] | None = None,
) -> Iterator[CopyFillEvent]:
    """Stream deduplicated Lane 1 wide events without retaining decoded JSON rows.

    `target_keys` is optional and is applied before an event is retained by the caller.
    Missing/invalid rows preserve the existing fail-skip parsing semantics; duplicate
    detection uses the same wallet + exchange timestamp + tid identity as
    `position_copy.load_wide_events`.
    """
    if not enriched_dir.exists():
        return

    normalized_targets = _normalized_target_keys(target_keys)
    seen: set[tuple[str, int, int]] = set()

    for path in sorted(enriched_dir.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict) or row.get("kind") != "wide_official_fill":
                    continue
                try:
                    received = int(row["public_received_at_ns"])
                except (KeyError, TypeError, ValueError):
                    continue
                if received < cutoff_ns:
                    continue
                raw = row.get("official_fill")
                if not isinstance(raw, dict):
                    continue
                address = str(row.get("wallet_address") or "").lower()
                event = _event_from_fill(
                    lane="WIDE",
                    wallet_id=str(row.get("wallet_id") or address),
                    wallet_address=address,
                    received_at_ns=received,
                    raw_fill=raw,
                )
                if event is None:
                    continue
                if normalized_targets is not None and (address, event.coin) not in normalized_targets:
                    continue
                key = (address, event.exchange_ts_ms, event.tid)
                if key in seen:
                    continue
                seen.add(key)
                yield event


class WideEventStream:
    """Single-pass sequence adapter for legacy callers that only iterate then call len()."""

    def __init__(
        self,
        enriched_dir: Path,
        *,
        cutoff_ns: int,
        target_keys: set[TargetKey] | None = None,
    ) -> None:
        self.enriched_dir = enriched_dir
        self.cutoff_ns = cutoff_ns
        self.target_keys = target_keys
        self._consumed = False
        self._count: int | None = None

    def __iter__(self) -> Iterator[CopyFillEvent]:
        if self._consumed:
            raise RuntimeError("WideEventStream is single-pass")
        self._consumed = True
        count = 0
        try:
            for event in iter_wide_events_memory_bounded(
                self.enriched_dir,
                cutoff_ns=self.cutoff_ns,
                target_keys=self.target_keys,
            ):
                count += 1
                yield event
        finally:
            self._count = count

    def __len__(self) -> int:
        if self._count is None:
            raise RuntimeError("WideEventStream length is available only after iteration")
        return self._count
