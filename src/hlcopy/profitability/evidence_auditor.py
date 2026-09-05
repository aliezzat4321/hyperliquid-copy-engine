"""Deterministic, fail-closed profitability evidence auditing.

The auditor validates evidence; it does not estimate economics or decide whether a
strategy is attractive. Unknown values remain unknown and block validated or
promotion-eligible verdicts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "profitability-evidence-audit-v1"
PROMOTION_ARTIFACT_VERSION = "profitability-promotion-artifact-v1"
AUDITOR_IDENTITY = "hlcopy.profitability.evidence_auditor"
POLICY_IDENTITY = "docs/ai-team/PROMOTION_POLICY.md"
POLICY_PATH = Path(__file__).parents[3] / "docs/ai-team/promotion_policy.json"
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
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _promotion_policy() -> dict[str, Any]:
    """Load the repository policy which defines the promotion floors."""
    try:
        policy = json.loads(POLICY_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("promotion policy is unavailable or malformed") from exc
    if not isinstance(policy, dict):
        raise ValueError("promotion policy must be an object")
    return policy


def _profit_concentration(bundle: dict[str, Any]) -> Decimal | None:
    """Calculate the largest closed-position profit share from row economics."""
    profits: list[Decimal] = []
    positions = bundle.get("positions")
    if not isinstance(positions, list):
        return None
    for row in positions:
        if not isinstance(row, dict) or row.get("status") != "closed":
            continue
        economics = row.get("economics")
        if not isinstance(economics, dict):
            return None
        gross = _decimal(economics.get("gross_pnl"))
        costs: list[Decimal] = []
        for name in MATERIAL_COSTS:
            item = economics.get(name)
            amount = _decimal(item.get("amount")) if isinstance(item, dict) else None
            if amount is None:
                return None
            costs.append(amount)
        funding = economics.get("funding")
        funding_amount = (
            _decimal(funding.get("amount")) if isinstance(funding, dict) else Decimal(0)
        )
        if gross is None or funding_amount is None:
            return None
        net = gross - sum(costs, Decimal(0)) - funding_amount
        if net > 0:
            profits.append(net)
    total_profit = sum(profits, Decimal(0))
    return max(profits) / total_profit if total_profit > 0 else None


def _economic_predicates(
    bundle: dict[str, Any], audit_report: dict[str, Any], policy: dict[str, Any]
) -> dict[str, bool]:
    """Derive promotion checks from sealed evidence, never caller assertions."""
    thresholds = policy.get("thresholds")
    risk_thresholds = policy.get("risk_governor", {}).get("thresholds")
    statistics = bundle.get("promotion_statistics")
    if not isinstance(thresholds, dict):
        thresholds = {}
    if not isinstance(risk_thresholds, dict):
        risk_thresholds = {}
    if not isinstance(statistics, dict):
        statistics = {}

    counts = audit_report.get("counts", {})
    closed = counts.get("closed")
    days = counts.get("trading_days")
    minimum_evidence = (
        isinstance(closed, int)
        and isinstance(days, int)
        and closed >= thresholds.get("min_closed_trades", float("inf"))
        and days >= thresholds.get("min_distinct_days", float("inf"))
    )

    primary = statistics.get("primary_metrics")
    mean_return = _decimal(primary.get("mean_return_bps")) if isinstance(primary, dict) else None
    lower_bound = _decimal(statistics.get("lower_bound_return_bps"))
    round_trip_cost = _decimal(statistics.get("round_trip_cost_bps"))
    confidence = _decimal(statistics.get("confidence_level"))
    concentration = _profit_concentration(bundle)
    uncertainty_method = statistics.get("uncertainty_method")
    multiple_testing = statistics.get("multiple_testing_treatment")
    total = counts.get("input")
    unresolved = counts.get("unresolved")
    unresolved_share = (
        Decimal(unresolved) / Decimal(total)
        if isinstance(total, int) and total > 0 and isinstance(unresolved, int)
        else None
    )
    estimated_capacity = _decimal(statistics.get("estimated_capacity_usd"))
    target_notional = _decimal(statistics.get("target_notional_usd"))
    required_confidence = _decimal(thresholds.get("confidence_level"))
    max_concentration = _decimal(thresholds.get("max_profit_concentration"))
    max_unresolved = _decimal(thresholds.get("max_unresolved_share"))
    minimum_capacity = _decimal(risk_thresholds.get("minimum_capacity_usd"))
    measured_costs = audit_report.get("economics_basis") == "MEASURED_COMPONENTS"

    return {
        "minimum_evidence": minimum_evidence,
        "primary_metrics": mean_return is not None,
        "success_criteria": bool(
            lower_bound is not None
            and round_trip_cost is not None
            and lower_bound > round_trip_cost
            and concentration is not None
            and max_concentration is not None
            and concentration <= max_concentration
        ),
        "uncertainty_method": bool(
            isinstance(uncertainty_method, str)
            and uncertainty_method.strip()
            and confidence is not None
            and required_confidence is not None
            and confidence >= required_confidence
        ),
        "multiple_testing_treatment": bool(
            isinstance(multiple_testing, str) and multiple_testing.strip()
        ),
        "execution_costs": bool(measured_costs and round_trip_cost is not None),
        "unresolved_exposure": bool(
            unresolved_share is not None
            and max_unresolved is not None
            and unresolved_share <= max_unresolved
        ),
        "capacity": bool(
            estimated_capacity is not None
            and target_notional is not None
            and minimum_capacity is not None
            and estimated_capacity >= minimum_capacity
            and target_notional <= estimated_capacity
        ),
    }


@dataclass(frozen=True)
class EconomicEvidenceArtifact:
    """Immutable canonical output of the economic evidence engine."""

    canonical_payload: bytes
    evidence_sha256: str


def build_economic_evidence_artifact(
    bundle: dict[str, Any],
    *,
    contract_fingerprint: str,
) -> EconomicEvidenceArtifact:
    """Audit evidence and seal the result and promotion bindings as canonical bytes."""
    audit_report = audit_evidence(bundle)
    policy = _promotion_policy()
    predicates = _economic_predicates(bundle, audit_report, policy)
    provenance = bundle.get("provenance")
    payload = {
        "artifact_version": PROMOTION_ARTIFACT_VERSION,
        "auditor": {"identity": AUDITOR_IDENTITY, "version": SCHEMA_VERSION},
        "policy": {
            "identity": POLICY_IDENTITY,
            "id": policy.get("policy_id"),
            "version": policy.get("policy_version"),
            "sha256": _canonical_hash(policy),
        },
        "contract_fingerprint": contract_fingerprint,
        "code_sha": provenance.get("code_commit") if isinstance(provenance, dict) else None,
        "data_sha256": (
            provenance.get("data_sha256") if isinstance(provenance, dict) else None
        ),
        "evaluation_window": bundle.get("evaluation_window"),
        "predicates": predicates,
        "evidence_bundle": bundle,
        "audit_report": audit_report,
    }
    canonical = _canonical_bytes(payload)
    return EconomicEvidenceArtifact(canonical, hashlib.sha256(canonical).hexdigest())


def verify_economic_evidence_artifact(
    artifact: object,
    *,
    contract_fingerprint: str | None,
    code_sha: object,
    data_sha256: object,
    evaluation_window: object,
    required_predicates: tuple[str, ...],
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Re-hash and re-audit an immutable artifact against exact promotion inputs."""
    if not isinstance(artifact, EconomicEvidenceArtifact):
        return None, ("DOWNSTREAM_ECONOMIC_ARTIFACT_REQUIRED",)
    blockers: list[str] = []
    actual_hash = hashlib.sha256(artifact.canonical_payload).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", artifact.evidence_sha256):
        blockers.append("ECONOMIC_ARTIFACT_HASH_INVALID")
    elif actual_hash != artifact.evidence_sha256:
        blockers.append("ECONOMIC_ARTIFACT_HASH_MISMATCH")
    try:
        payload = json.loads(artifact.canonical_payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, tuple((*blockers, "ECONOMIC_ARTIFACT_PAYLOAD_INVALID"))
    if not isinstance(payload, dict):
        return None, tuple((*blockers, "ECONOMIC_ARTIFACT_PAYLOAD_INVALID"))
    expected_fields = {
        "artifact_version",
        "auditor",
        "policy",
        "contract_fingerprint",
        "code_sha",
        "data_sha256",
        "evaluation_window",
        "predicates",
        "evidence_bundle",
        "audit_report",
    }
    if set(payload) != expected_fields:
        blockers.append("ECONOMIC_ARTIFACT_SCHEMA_INVALID")
    if payload.get("artifact_version") != PROMOTION_ARTIFACT_VERSION:
        blockers.append("ECONOMIC_ARTIFACT_VERSION_MISMATCH")
    if payload.get("auditor") != {"identity": AUDITOR_IDENTITY, "version": SCHEMA_VERSION}:
        blockers.append("ECONOMIC_AUDITOR_IDENTITY_MISMATCH")
    bundle = payload.get("evidence_bundle")
    recorded_report = payload.get("audit_report")
    if not isinstance(bundle, dict) or not isinstance(recorded_report, dict):
        blockers.append("ECONOMIC_ARTIFACT_EVIDENCE_INVALID")
        recomputed_report = None
    else:
        recomputed_report = audit_evidence(bundle)
        if recomputed_report != recorded_report:
            blockers.append("ECONOMIC_AUDIT_RECOMPUTATION_MISMATCH")
    try:
        policy = _promotion_policy()
    except ValueError:
        policy = {}
        blockers.append("ECONOMIC_POLICY_UNAVAILABLE")
    expected_policy = {
        "identity": POLICY_IDENTITY,
        "id": policy.get("policy_id"),
        "version": policy.get("policy_version"),
        "sha256": _canonical_hash(policy),
    }
    expected_bundle_version = (
        f"{policy.get('policy_id')}-{policy.get('policy_version')}" if policy else None
    )
    if (
        payload.get("policy") != expected_policy
        or not expected_policy["version"]
        or not isinstance(bundle, dict)
        or bundle.get("policy_version") != expected_bundle_version
    ):
        blockers.append("ECONOMIC_POLICY_IDENTITY_MISMATCH")
    exact_bindings = (
        ("contract_fingerprint", contract_fingerprint),
        ("code_sha", code_sha),
        ("data_sha256", data_sha256),
        ("evaluation_window", evaluation_window),
    )
    for name, expected in exact_bindings:
        if payload.get(name) != expected:
            blockers.append(f"ECONOMIC_ARTIFACT_{name.upper()}_MISMATCH")
    predicates = payload.get("predicates")
    recomputed_predicates = (
        _economic_predicates(bundle, recomputed_report, policy)
        if isinstance(bundle, dict) and recomputed_report is not None and policy
        else None
    )
    if (
        not isinstance(predicates, dict)
        or set(predicates) != set(required_predicates)
        or predicates != recomputed_predicates
    ):
        blockers.append("ECONOMIC_AUDIT_PREDICATES_INVALID")
    else:
        blockers.extend(
            f"ECONOMIC_AUDIT_{name.upper()}_NOT_SATISFIED"
            for name in required_predicates
            if predicates.get(name) is not True
        )
    if recomputed_report is None or recomputed_report.get("status") != "PASS" or (
        recomputed_report.get("promotion_eligible") is not True
    ):
        blockers.append("DOWNSTREAM_ECONOMIC_AUDIT_NOT_PASSING")
    return payload, tuple(dict.fromkeys(blockers))


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
    else:
        if not re.fullmatch(r"[0-9a-f]{64}", str(provenance["data_sha256"])):
            block(
                "DATA_HASH_INVALID",
                "CORRUPTED_EVIDENCE",
                "data_sha256 must be an exact lowercase SHA-256",
            )
        if not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", str(provenance["code_commit"])
        ):
            block(
                "CODE_COMMIT_INVALID",
                "CORRUPTED_EVIDENCE",
                "code_commit must be an exact lowercase Git commit SHA",
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
    if start is None or end is None or start >= end:
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
    max_age_d = _decimal(bundle.get("max_data_age_seconds"))
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
    selection_known = isinstance(selection, dict) and isinstance(selection.get("prospective"), bool)
    if not selection_known:
        block(
            "SELECTION_STATE_MISSING",
            "MISSING_EVIDENCE",
            "selection.prospective must explicitly be true or false",
        )
    prospective = bool(selection_known and selection.get("prospective") is True)
    frozen_at = _time(selection.get("frozen_at")) if isinstance(selection, dict) else None
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
        block(
            "MALFORMED_ROWS",
            "CORRUPTED_EVIDENCE",
            f"{malformed} position rows are not objects",
        )

    ids = [str(row.get("position_id") or "") for row in positions]
    duplicate_ids = sorted(key for key, count in Counter(ids).items() if key and count > 1)
    if duplicate_ids:
        block(
            "DUPLICATE_POSITIONS",
            "CORRUPTED_EVIDENCE",
            f"{len(duplicate_ids)} duplicate position IDs",
        )
    if any(not key for key in ids):
        block(
            "MALFORMED_POSITION_ID",
            "CORRUPTED_EVIDENCE",
            "every position requires position_id",
        )

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
    assumed_cost_components: set[str] = set()
    economics_complete = True
    closed = unresolved = missing_outcomes = orphan_count = malformed_close_count = 0
    trading_days: set[str] = set()

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
        ordered: list[datetime] = []
        for name in ("signal", "decision", "shadow_or_execution", "open", "close"):
            if timestamps.get(name) is None:
                continue
            parsed = _time(timestamps[name])
            if parsed is None:
                block(
                    "MALFORMED_TIMESTAMP",
                    "CORRUPTED_EVIDENCE",
                    f"position {row.get('position_id')} has invalid {name} timestamp",
                )
                continue
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
        if ordered:
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
                    if item["basis"] == "assumption":
                        assumed_cost_components.add(cost)
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
    expected_value = expected.get("input_count") if isinstance(expected, dict) else None
    expected_inputs = (
        expected_value
        if isinstance(expected_value, int)
        and not isinstance(expected_value, bool)
        and expected_value >= 0
        else None
    )
    if expected_inputs is None:
        block(
            "POPULATION_BASELINE_MISSING",
            "MISSING_EVIDENCE",
            "an independent population.input_count non-negative integer is required",
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
    )
    final_net = calculated_net if economics_complete else None
    if not isinstance(declared, dict) or _decimal(declared.get("final_net")) is None:
        block(
            "FINAL_NET_MISSING",
            "MISSING_EVIDENCE",
            "economics_totals.final_net is required",
        )
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

    promotion_ready = (
        not blockers
        and prospective
        and not assumed_cost_components
        and final_net is not None
        and final_net > 0
    )
    bounded_blockers = sorted(blockers, key=lambda item: (item["code"], item["detail"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if not blockers else "FAIL",
        "promotion_eligible": promotion_ready,
        "validated_profitability_allowed": promotion_ready,
        "economics_state": economics_state,
        "economics_basis": (
            "SCENARIO_ASSUMPTIONS" if assumed_cost_components else "MEASURED_COMPONENTS"
        ),
        "assumed_cost_components": sorted(assumed_cost_components),
        "versions": {"report": report_version, "policy": policy_version},
        "provenance": provenance,
        "evaluation_window": window,
        "counts": {
            "input": len(positions),
            "closed": closed,
            "unresolved": unresolved,
            "missing_outcomes": missing_outcomes,
            "malformed_rows": malformed,
            "malformed_closes": malformed_close_count,
            "duplicate_ids": len(duplicate_ids),
            "orphans": orphan_count,
            "trading_days": len(trading_days),
        },
        "economics": {
            **{key: str(value) for key, value in totals.items()},
            "final_net": str(final_net) if final_net is not None else None,
            "unresolved_mtm_included_in_final_net": False,
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
                float(milliseconds / Decimal(1000)), UTC
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
            positions.append(
                {
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
                }
            )
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
    # Population is source evidence, not an adapter output. In particular, deriving
    # it from ``positions`` would make a truncated stream reconcile with itself.
    # Spreading the manifest preserves a supplied baseline verbatim and leaves an
    # absent baseline absent so that audit_evidence can fail closed while still
    # returning position diagnostics.
    return {**manifest, "positions": positions}


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
