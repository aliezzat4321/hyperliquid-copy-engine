from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

D = Decimal


class Disposition(StrEnum):
    VALID_CLOSED = "VALID_CLOSED"
    OPEN = "OPEN"
    QUARANTINE_UNPRICED_CLOSE = "QUARANTINE_UNPRICED_CLOSE"
    QUARANTINE_LEG_MISMATCH = "QUARANTINE_LEG_MISMATCH"
    QUARANTINE_DUPLICATE_REPROCESSED = "QUARANTINE_DUPLICATE_REPROCESSED"
    CAPACITY_INFEASIBLE = "CAPACITY_INFEASIBLE"


class OrphanCause(StrEnum):
    OPEN_SKIPPED_STALE = "OPEN_SKIPPED_STALE"
    OPEN_SKIPPED_UNKNOWN_ASSET = "OPEN_SKIPPED_UNKNOWN_ASSET"
    OPEN_SKIPPED_LEVERAGE = "OPEN_SKIPPED_LEVERAGE"
    OPEN_PREDATES_LEDGER = "OPEN_PREDATES_LEDGER"
    OPEN_LOST_AT_BASELINE = "OPEN_LOST_AT_BASELINE"
    TRUE_ORPHAN = "TRUE_ORPHAN"


@dataclass(frozen=True, slots=True)
class ExecutionLeg:
    timestamp_ms: int
    mid: D
    size: D
    notional: D
    kind: str
    signal_key: str = ""


@dataclass(slots=True)
class ReconstructedPosition:
    source_base_id: str
    trader: str
    coin: str
    side: str
    entry_legs: list[ExecutionLeg]
    exit_leg: ExecutionLeg | None = None
    disposition: Disposition = Disposition.OPEN
    detection_latencies_ms: list[float] = field(default_factory=list)
    chase_bps: list[float] = field(default_factory=list)
    source_entry_price: D | None = None
    source_closing_price: D | None = None
    return_on_margin_pct: D | None = None
    quarantine_sensitivity_usd: D | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def direction(self) -> D:
        return D("1") if self.side.lower() == "long" else D("-1")

    @property
    def entry_notional(self) -> D:
        return sum((leg.notional for leg in self.entry_legs), D("0"))

    @property
    def final_size(self) -> D:
        return sum((leg.size for leg in self.entry_legs), D("0"))

    def validate_blended_notional(self, blended_mid: D, final_size: D) -> bool:
        expected = blended_mid * final_size
        scale = max(abs(expected), abs(self.entry_notional), D("1"))
        return abs(expected - self.entry_notional) / scale <= D("1e-6")


def event_ms(row: dict[str, Any]) -> int:
    from datetime import datetime

    value = row.get("ts") or row.get("timestamp")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    raise ValueError("audit event lacks a valid timestamp")


def dec(value: Any) -> D:
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("non-finite economic value")
    return result
