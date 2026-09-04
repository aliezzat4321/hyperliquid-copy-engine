from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .reconstruction import (
    Disposition,
    ExecutionLeg,
    OrphanCause,
    ReconstructedPosition,
    dec,
    event_ms,
)


class ReconciliationError(RuntimeError):
    pass


@dataclass(slots=True)
class LedgerResult:
    positions: list[ReconstructedPosition] = field(default_factory=list)
    orphan_causes: dict[str, OrphanCause] = field(default_factory=dict)
    duplicate_close_rows: int = 0
    duplicate_key_reprocessed: int = 0
    close_signals_observed: int = 0
    opens_recorded: int = 0
    unpriced_closes: int = 0
    orphan_closes: int = 0

    def reconcile(self) -> dict[str, int]:
        counts = {item: 0 for item in Disposition}
        for position in self.positions:
            counts[position.disposition] += 1
        i1_rhs = (
            counts[Disposition.VALID_CLOSED]
            + counts[Disposition.OPEN]
            + counts[Disposition.QUARANTINE_UNPRICED_CLOSE]
            + counts[Disposition.QUARANTINE_LEG_MISMATCH]
            + counts[Disposition.QUARANTINE_DUPLICATE_REPROCESSED]
        )
        i2_rhs = (
            counts[Disposition.VALID_CLOSED]
            + self.duplicate_close_rows
            + self.unpriced_closes
            + self.orphan_closes
        )
        if self.opens_recorded != i1_rhs or self.close_signals_observed != i2_rhs:
            raise ReconciliationError(
                f"reconciliation failed: I1 {self.opens_recorded}!={i1_rhs}; "
                f"I2 {self.close_signals_observed}!={i2_rhs}"
            )
        return {"opens_recorded": self.opens_recorded, "i1_rhs": i1_rhs,
                "close_signals_observed": self.close_signals_observed, "i2_rhs": i2_rhs}


def load_audit_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReconciliationError(f"malformed audit JSON at line {number}") from exc
        if not isinstance(row, dict):
            raise ReconciliationError(f"non-object audit row at line {number}")
        rows.append(row)
    return rows


def _base_id(row: dict[str, Any]) -> str:
    signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
    managed = row.get("managed") if isinstance(row.get("managed"), dict) else {}
    return str(
        row.get("sourceBaseId")
        or signal.get("sourceBaseId")
        or managed.get("sourceBaseId")
        or ""
    )


def _signal_key(row: dict[str, Any]) -> str:
    signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
    return str(signal.get("key") or "")


def _classify_orphan(rows: list[dict[str, Any]], close_index: int, base_id: str) -> OrphanCause:
    close_ms = event_ms(rows[close_index])
    earlier = rows[:close_index]
    for row in reversed(earlier):
        if _base_id(row) != base_id:
            continue
        reason = row.get("reason")
        if reason == "stale_signal_over_25s_window":
            return OrphanCause.OPEN_SKIPPED_STALE
        if reason == "unknown_hl_asset":
            return OrphanCause.OPEN_SKIPPED_UNKNOWN_ASSET
        if reason == "source_leverage_unexecutable_on_hl":
            return OrphanCause.OPEN_SKIPPED_LEVERAGE
    starts = [event_ms(row) for row in rows if row.get("type") == "service_started"]
    if starts and close_ms < min(starts):
        return OrphanCause.OPEN_PREDATES_LEDGER
    if any(row.get("type") == "baseline_indexed" for row in earlier):
        return OrphanCause.OPEN_LOST_AT_BASELINE
    return OrphanCause.TRUE_ORPHAN


def reconstruct_ledger(
    rows: list[dict[str, Any]], state: dict[str, Any] | None = None
) -> LedgerResult:
    result = LedgerResult()
    by_id: dict[str, ReconstructedPosition] = {}
    seen_keys: dict[str, str] = {}
    closed_ids: set[str] = set()
    for index, row in enumerate(rows):
        kind = str(row.get("type") or "")
        base_id = _base_id(row)
        key = _signal_key(row)
        if key:
            prior = seen_keys.get(key)
            if prior is not None and kind in {
                "shadow_opened",
                "shadow_opened_from_increase",
                "shadow_reupped",
            }:
                result.duplicate_key_reprocessed += 1
                if base_id in by_id:
                    by_id[base_id].disposition = Disposition.QUARANTINE_DUPLICATE_REPROCESSED
                continue
            seen_keys[key] = kind
        if kind in {"shadow_opened", "shadow_opened_from_increase"}:
            result.opens_recorded += 1
            signal = row.get("signal") or {}
            leg = ExecutionLeg(event_ms(row), dec(row["entryMid"]), dec(row["size"]),
                               dec(row["notionalUsd"]), "ENTRY", key)
            by_id[base_id] = ReconstructedPosition(
                base_id, str(signal.get("username") or row.get("username") or ""),
                str(signal.get("coin") or row.get("coin") or "").upper(),
                str(signal.get("side") or row.get("side") or ""), [leg],
                source_entry_price=dec(signal["entryPrice"]) if signal.get("entryPrice") else None,
            )
            if row.get("detectionLatencyMs") is not None:
                by_id[base_id].detection_latencies_ms.append(float(row["detectionLatencyMs"]))
            if row.get("chaseBps") is not None:
                by_id[base_id].chase_bps.append(float(row["chaseBps"]))
        elif kind == "shadow_reupped" and base_id in by_id:
            by_id[base_id].entry_legs.append(ExecutionLeg(
                event_ms(row), dec(row["addMid"]), dec(row["addedSize"]),
                dec(row["addNotionalUsd"]), "ENTRY", key))
            if row.get("detectionLatencyMs") is not None:
                by_id[base_id].detection_latencies_ms.append(float(row["detectionLatencyMs"]))
            if row.get("chaseBps") is not None:
                by_id[base_id].chase_bps.append(float(row["chaseBps"]))
        elif kind in {"shadow_closed", "shadow_close_unpriced"} or (
            kind == "skip" and row.get("reason") == "close_not_owned_by_service"
        ):
            result.close_signals_observed += 1
            if kind == "shadow_closed" and base_id in closed_ids:
                result.duplicate_close_rows += 1
                continue
            if kind == "shadow_closed" and base_id in by_id:
                pos = by_id[base_id]
                pos.exit_leg = ExecutionLeg(event_ms(row), dec(row["exitMid"]), dec(row["size"]),
                                             dec(row["exitMid"]) * dec(row["size"]), "EXIT", key)
                pos.source_closing_price = (
                    dec(row["sourceClosingPrice"]) if row.get("sourceClosingPrice") else None
                )
                if row.get("detectionLatencyMs") is not None:
                    pos.detection_latencies_ms.append(float(row["detectionLatencyMs"]))
                pos.return_on_margin_pct = (
                    dec(row["returnOnMarginPct"])
                    if row.get("returnOnMarginPct") is not None
                    else None
                )
                if not pos.validate_blended_notional(dec(row["entryMid"]), dec(row["size"])):
                    pos.disposition = Disposition.QUARANTINE_LEG_MISMATCH
                else:
                    pos.disposition = Disposition.VALID_CLOSED
                closed_ids.add(base_id)
            elif kind == "shadow_close_unpriced" and base_id in by_id:
                result.unpriced_closes += 1
                pos = by_id[base_id]
                pos.disposition = Disposition.QUARANTINE_UNPRICED_CLOSE
                signal = row.get("signal") or {}
                if signal.get("closingPrice") is not None:
                    close = dec(signal["closingPrice"])
                    pos.quarantine_sensitivity_usd = sum(
                        (close - leg.mid) * leg.size * pos.direction for leg in pos.entry_legs
                    )
            else:
                result.orphan_closes += 1
                result.orphan_causes[base_id or f"row:{index}"] = _classify_orphan(
                    rows, index, base_id
                )
    managed = (state or {}).get("managed", {})
    for base_id, pos in by_id.items():
        if pos.disposition == Disposition.OPEN and base_id not in managed:
            # Absence from state is itself an accounting exception, never silently closed.
            pos.disposition = Disposition.QUARANTINE_DUPLICATE_REPROCESSED
        result.positions.append(pos)
    result.reconcile()
    return result
