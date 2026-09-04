"""Deterministic, fail-closed profitability evidence auditing.

The auditor validates evidence; it does not estimate economics or decide whether a
strategy is attractive.  Unknown values remain unknown and block validated or
promotion-eligible verdicts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "profitability-evidence-audit-v1"
VALID_STATUSES = {"closed", "open", "unresolved", "quarantined"}
MATERIAL_COSTS = ("fees", "spread", "depth", "slippage", "impact")
TOLERANCE = Decimal("0.00000001")


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def audit_evidence(bundle: dict[str, Any]) -> dict[str, Any]:
    """Audit a normalized evidence bundle and return a bounded JSON-safe report."""
    blockers: list[dict[str, str]] = []
    counts: Counter[str] = Counter()

    def block(code: str, classification: str, detail: str) -> None:
        blockers.append({"code": code, "classification": classification, "detail": detail})
        counts[classification] += 1

    provenance = bundle.get("provenance")
    if not isinstance(provenance, dict) or not all(
        provenance.get(key) for key in ("source", "data_sha256", "code_commit")
    ):
        block(
            "PROVENANCE_INCOMPLETE",
            "MISSING_EVIDENCE",
            "source, data_sha256 and code_commit are required",
        )
    elif not re.fullmatch(r"[0-9a-f]{64}", str(provenance["data_sha256"])):
        block(
            "DATA_HASH_INVALID",
            "CORRUPTED_EVIDENCE",
            "data_sha256 must be an exact lowercase SHA-256",
        )

    report_version = bundle.get("report_version")
    policy_version = bundle.get("policy_version")
    if not all(isinstance(value, str) and value for value in (report_version, policy_version)):
        block(
            "VERSION_INCOMPLETE",
            "MISSING_EVIDENCE",
            "report_version and policy_version are required",
        )

    window = bundle.get("evaluation_window")
    start = _time(window.get("start")) if isinstance(window, dict) else None
    end = _time(window.get("end")) if isinstance(window, dict) else None
    audited_at = _time(bundle.get("audited_at"))
    if start is None or end is None or start > end:
        block(
            "EVALUATION_WINDOW_INVALID",
            "CORRUPTED_EVIDENCE",
            "evaluation start/end must be valid ordered UTC timestamps",
        )
    if audited_at is None:
        block(
            "AUDIT_TIMESTAMP_INVALID",
            "CORRUPTED_EVIDENCE",
            "audited_at must be a timezone-aware timestamp",
        )
    max_age = bundle.get("max_data_age_seconds")
    max_age_d = _decimal(max_age)
    if max_age_d is None or max_age_d < 0:
        block(
            "FRESHNESS_LIMIT_MISSING",
            "MISSING_EVIDENCE",
            "max_data_age_seconds must be a non-negative number",
        )
    age_seconds = Decimal(str((audited_at - end).total_seconds())) if end and audited_at else None
    if end and audited_at and (
        end > audited_at or (max_age_d is not None and age_seconds > max_age_d)
    ):
        code = "FUTURE_DATA" if end > audited_at else "STALE_DATA"
        classification = "CORRUPTED_EVIDENCE" if code == "FUTURE_DATA" else "MISSING_EVIDENCE"
        block(
            code,
            classification,
            "evaluation end is impossible or older than the declared freshness limit",
        )

    selection = bundle.get("selection")
    frozen_at = _time(selection.get("frozen_at")) if isinstance(selection, dict) else None
    prospective = bool(isinstance(selection, dict) and selection.get("prospective"))
    if prospective and frozen_at is None:
        block(
            "SELECTION_FREEZE_MISSING",
            "MISSING_EVIDENCE",
            "prospective evidence requires frozen_at",
        )
    if prospective and frozen_at and start and frozen_at >= start:
        block(
            "SAME_WINDOW_LEAKAGE",
            "CORRUPTED_EVIDENCE",
            "selection must be frozen before the evaluation window",
        )

    if not isinstance(bundle.get("funding_applicable"), bool):
        block(
            "FUNDING_APPLICABILITY_MISSING",
            "MISSING_EVIDENCE",
            "funding_applicable must explicitly be true or false",
        )

    raw_positions = bundle.get("positions")
    if not isinstance(raw_positions, list):
        raw_positions = []
        block("POSITIONS_MISSING", "MISSING_EVIDENCE", "positions must be an array")
    positions = [row for row in raw_positions if isinstance(row, dict)]
    malformed = len(raw_positions) - len(positions)
    if malformed:
        block("MALFORMED_ROWS", "CORRUPTED_EVIDENCE", f"{malformed} position rows are not objects")

    ids = [str(row.get("position_id") or "") for row in positions]
    duplicate_ids = sorted(key for key, count in Counter(ids).items() if key and count > 1)
    if duplicate_ids:
        block(
            "DUPLICATE_POSITIONS",
            "CORRUPTED_EVIDENCE",
            f"{len(duplicate_ids)} duplicate position IDs",
        )
    if any(not key for key in ids):
        block("MALFORMED_POSITION_ID", "CORRUPTED_EVIDENCE", "every position requires position_id")

    totals = {
        "gross_pnl": Decimal(0),
        "fees": Decimal(0),
        "spread": Decimal(0),
        "depth": Decimal(0),
        "slippage": Decimal(0),
        "impact": Decimal(0),
        "funding": Decimal(0),
        "unresolved_mtm": Decimal(0),
    }
    economics_complete = True
    closed = unresolved = missing_outcomes = orphan_count = malformed_close_count = 0
    trading_days: set[str] = set()
    last_event: datetime | None = None

    for row in positions:
        status = str(row.get("status") or "").lower()
        if status not in VALID_STATUSES:
            missing_outcomes += 1
            block(
                "POSITION_DISAPPEARED",
                "CORRUPTED_EVIDENCE",
                f"position {row.get('position_id')} has no valid outcome classification",
            )
        if row.get("orphan") is True:
            orphan_count += 1
        timestamps = row.get("timestamps")
        timestamps = timestamps if isinstance(timestamps, dict) else {}
        required_timestamps = ["signal", "decision", "shadow_or_execution", "open"]
        if status == "closed":
            required_timestamps.append("close")
        missing_timestamps = [name for name in required_timestamps if timestamps.get(name) is None]
        if missing_timestamps:
            block(
                "LIFECYCLE_STAGE_MISSING",
                "MISSING_EVIDENCE",
                f"position {row.get('position_id')} lacks {','.join(missing_timestamps)}",
            )
        ordered = []
        for name in ("signal", "decision", "shadow_or_execution", "open", "close"):
            if timestamps.get(name) is not None:
                parsed = _time(timestamps[name])
                if parsed is None:
                    block(
                        "MALFORMED_TIMESTAMP",
                        "CORRUPTED_EVIDENCE",
                        f"position {row.get('position_id')} has invalid {name} timestamp",
                    )
                else:
                    ordered.append(parsed)
                    if start and end and (parsed < start or parsed > end):
                        block(
                            "EVENT_OUTSIDE_WINDOW",
                            "CORRUPTED_EVIDENCE",
                            f"position {row.get('position_id')} {name} lies outside window",
                        )
                    if audited_at and parsed > audited_at:
                        block(
                            "FUTURE_EVENT",
                            "CORRUPTED_EVIDENCE",
                            f"position {row.get('position_id')} {name} is after audited_at",
                        )
        if ordered != sorted(ordered):
            block(
                "TIMESTAMP_NON_MONOTONIC",
                "CORRUPTED_EVIDENCE",
                f"position {row.get('position_id')} lifecycle timestamps are non-monotonic",
            )
        if ordered and last_event and ordered[0] < last_event:
            block(
                "LEDGER_NON_MONOTONIC",
                "CORRUPTED_EVIDENCE",
                "position rows are not in append-time order",
            )
        if ordered:
            last_event = ordered[-1]
            trading_days.add(ordered[0].date().isoformat())

        economics = row.get("economics")
        economics = economics if isinstance(economics, dict) else {}
        if status == "closed":
            closed += 1
            close_invalid = _time(timestamps.get("close")) is None
            gross_invalid = _decimal(economics.get("gross_pnl")) is None
            if close_invalid or gross_invalid:
                economics_complete = False
                malformed_close_count += 1
                block(
                    "MALFORMED_CLOSE",
                    "CORRUPTED_EVIDENCE",
                    f"closed position {row.get('position_id')} lacks close time or gross PnL",
                )
            else:
                totals["gross_pnl"] += _decimal(economics["gross_pnl"]) or Decimal(0)
            for cost in MATERIAL_COSTS:
                item = economics.get(cost)
                if (
                    not isinstance(item, dict)
                    or item.get("basis") not in {"measured", "assumption"}
                    or _decimal(item.get("amount")) is None
                ):
                    economics_complete = False
                    block(
                        f"{cost.upper()}_EVIDENCE_MISSING",
                        "MISSING_EVIDENCE",
                        f"closed position {row.get('position_id')} lacks labelled {cost}",
                    )
                else:
                    totals[cost] += _decimal(item["amount"]) or Decimal(0)
            funding = economics.get("funding")
            if bundle.get("funding_applicable"):
                if (
                    not isinstance(funding, dict)
                    or funding.get("coverage") != "complete"
                    or _decimal(funding.get("amount")) is None
                ):
                    economics_complete = False
                    block(
                        "FUNDING_COVERAGE_MISSING",
                        "MISSING_EVIDENCE",
                        f"closed position {row.get('position_id')} lacks funding coverage",
                    )
                else:
                    totals["funding"] += _decimal(funding["amount"]) or Decimal(0)
        elif status in {"open", "unresolved", "quarantined"}:
            unresolved += 1
            mtm = _decimal(economics.get("unresolved_mtm"))
            if mtm is None:
                economics_complete = False
                block(
                    "UNRESOLVED_MTM_MISSING",
                    "MISSING_EVIDENCE",
                    f"position {row.get('position_id')} has no unresolved MTM",
                )
            else:
                totals["unresolved_mtm"] += mtm

    if orphan_count:
        block(
            "ORPHAN_ROWS",
            "CORRUPTED_EVIDENCE",
            f"{orphan_count} positions are classified as orphans",
        )

    expected = bundle.get("population")
    try:
        expected_inputs = int(expected.get("input_count", -1)) if isinstance(expected, dict) else -1
    except (TypeError, ValueError):
        expected_inputs = -1
    if expected_inputs < 0:
        block(
            "POPULATION_BASELINE_MISSING",
            "MISSING_EVIDENCE",
            "population.input_count is required",
        )
    elif expected_inputs != len(positions):
        block(
            "POPULATION_UNRECONCILED",
            "CORRUPTED_EVIDENCE",
            f"input_count={expected_inputs}, classified={len(positions)}",
        )

    declared = bundle.get("economics_totals")
    calculated_net = (
        totals["gross_pnl"]
        - sum((totals[name] for name in MATERIAL_COSTS), Decimal(0))
        - totals["funding"]
        + totals["unresolved_mtm"]
    )
    final_net = calculated_net if economics_complete else None
    if not isinstance(declared, dict) or _decimal(declared.get("final_net")) is None:
        block("FINAL_NET_MISSING", "MISSING_EVIDENCE", "economics_totals.final_net is required")
    elif final_net is not None and abs(
        (_decimal(declared["final_net"]) or Decimal(0)) - final_net
    ) > TOLERANCE:
        block(
            "PNL_UNRECONCILED",
            "CORRUPTED_EVIDENCE",
            f"declared final_net does not reconcile to calculated {final_net}",
        )

    if counts["CORRUPTED_EVIDENCE"]:
        economics_state = "CORRUPTED_OR_UNRECONCILED"
    elif counts["MISSING_EVIDENCE"]:
        economics_state = "UNKNOWN_MISSING_EVIDENCE"
    elif final_net is not None and final_net <= 0:
        economics_state = "ZERO_OR_NEGATIVE_ECONOMICS"
    else:
        economics_state = "RECONCILED_POSITIVE_ECONOMICS"

    bounded_blockers = sorted(blockers, key=lambda item: (item["code"], item["detail"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if not blockers else "FAIL",
        "promotion_eligible": not blockers and final_net is not None and final_net > 0,
        "validated_profitability_allowed": not blockers and final_net is not None and final_net > 0,
        "economics_state": economics_state,
        "versions": {"report": report_version, "policy": policy_version},
        "provenance": provenance,
        "evaluation_window": window,
        "counts": {
            "input": len(positions), "closed": closed, "unresolved": unresolved,
            "missing_outcomes": missing_outcomes, "malformed_rows": malformed,
            "malformed_closes": malformed_close_count, "duplicate_ids": len(duplicate_ids),
            "orphans": orphan_count, "trading_days": len(trading_days),
        },
        "economics": {
            **{key: str(value) for key, value in totals.items()},
            "final_net": str(final_net) if final_net is not None else None,
        },
        "blocker_summary": dict(sorted(counts.items())),
        "blockers": bounded_blockers[:100],
        "diagnostics_truncated": len(bounded_blockers) > 100,
        "evidence_sha256": _canonical_hash(bundle),
    }


def lane3_bundle(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    """Normalize the append-only Lane 3 audit stream without inventing evidence."""
    opens: dict[str, dict[str, Any]] = {}
    positions: list[dict[str, Any]] = []

    def source_time(row: dict[str, Any]) -> str | None:
        signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
        value = signal.get("sourceTime")
        if isinstance(value, str):
            return value
        milliseconds = _decimal(signal.get("sourceTimeMs"))
        if milliseconds is not None:
            return datetime.fromtimestamp(
                float(milliseconds / Decimal(1000)), timezone.utc
            ).isoformat()
        return None

    for row in rows:
        row_type = row.get("type")
        signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
        position_id = str(row.get("sourceBaseId") or signal.get("sourceBaseId") or "")
        if row_type in {"shadow_opened", "shadow_opened_from_increase"}:
            if position_id in opens:
                positions.append(
                    {
                        "position_id": position_id,
                        "status": "quarantined",
                        "orphan": False,
                        "timestamps": {"open": row.get("ts")},
                        "economics": {},
                    }
                )
            opens[position_id] = row
        elif row_type in {"shadow_closed", "shadow_close_unpriced"}:
            opened = opens.pop(position_id, None)
            economics: dict[str, Any] = {"gross_pnl": row.get("grossPnlUsd")}
            economics.update(manifest.get("position_economics", {}).get(position_id, {}))
            positions.append({
                "position_id": position_id,
                "status": "closed",
                "orphan": opened is None,
                "timestamps": {
                    "signal": source_time(opened or {}),
                    "decision": (opened or {}).get("ts"),
                    "shadow_or_execution": (opened or {}).get("ts"),
                    "open": (opened or {}).get("ts"),
                    "close": row.get("ts"),
                },
                "economics": economics,
            })
        elif row_type == "malformed":
            positions.append(
                {
                    "position_id": f"malformed-line-{row.get('line_number')}",
                    "status": "quarantined",
                    "orphan": True,
                    "timestamps": {},
                    "economics": {},
                }
            )
    for position_id, opened in opens.items():
        supplied = manifest.get("position_economics", {}).get(position_id, {})
        positions.append(
            {
                "position_id": position_id,
                "status": "unresolved",
                "orphan": False,
                "timestamps": {
                    "signal": source_time(opened),
                    "decision": opened.get("ts"),
                    "shadow_or_execution": opened.get("ts"),
                    "open": opened.get("ts"),
                },
                "economics": supplied,
            }
        )
    return {**manifest, "positions": positions, "population": {"input_count": len(positions)}}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            row = {"type": "malformed", "line_number": line_number}
        rows.append(
            row
            if isinstance(row, dict)
            else {"type": "malformed", "line_number": line_number}
        )
    return rows
