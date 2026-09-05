"""Deterministic, fail-closed experiment freeze and evidence validation.

The controller operates on JSON-compatible mappings so every research lane can reuse
one ex-ante contract and registry-lock mechanism. A report cannot prove its own lock:
the frozen fingerprint must exist independently in the experiment registry before any
evaluation can be considered prospective-valid.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from hlcopy.profitability.evidence_auditor import verify_economic_evidence_artifact

Verdict = Literal[
    "EXPLORATORY_UNFROZEN",
    "FROZEN_NOT_YET_PROSPECTIVE",
    "PROSPECTIVE_CONTRACT_VALID",
    "INVALID_CONTRACT_DRIFT_OR_EVIDENCE_LEAKAGE",
]

ECONOMIC_GATE_CHECKS = (
    "minimum_evidence",
    "primary_metrics",
    "success_criteria",
    "uncertainty_method",
    "multiple_testing_treatment",
    "execution_costs",
    "unresolved_exposure",
    "capacity",
)

CONTRACT_FIELDS = (
    "contract_version", "experiment_id", "hypothesis", "causal_rationale",
    "lane", "strategy_id", "eligible_universe", "selection_rule",
    "decision_time_features", "parameters", "parameter_grid", "discovery_window",
    "prospective_start", "execution_cost_model", "target_notional_capacity",
    "minimum_evidence", "primary_metrics", "uncertainty_method",
    "multiple_testing_treatment", "success_criteria", "failure_criteria",
    "abandonment_criteria", "code_provenance", "data_provenance", "frozen_at",
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_contract_content(contract: Mapping[str, Any]) -> None:
    version = contract["contract_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ValueError("contract_version must be a positive integer")
    text_fields = (
        "experiment_id", "hypothesis", "causal_rationale", "lane", "strategy_id",
        "uncertainty_method", "multiple_testing_treatment", "abandonment_criteria",
    )
    for field in text_fields:
        if not isinstance(contract[field], str) or not contract[field].strip():
            raise ValueError(f"{field} must be non-empty text")
    mapping_fields = (
        "eligible_universe", "selection_rule", "parameters", "parameter_grid",
        "execution_cost_model", "target_notional_capacity", "minimum_evidence",
        "success_criteria", "failure_criteria", "code_provenance", "data_provenance",
    )
    for field in mapping_fields:
        if not isinstance(contract[field], Mapping) or not contract[field]:
            raise ValueError(f"{field} must be a non-empty object")
    features, metrics = contract["decision_time_features"], contract["primary_metrics"]
    if not isinstance(features, list) or not features:
        raise ValueError("decision_time_features must be a non-empty list")
    if not isinstance(metrics, list) or not metrics:
        raise ValueError("primary_metrics must be a non-empty list")
    sha = contract["code_provenance"].get("commit_sha")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise ValueError("code_provenance.commit_sha must be a 40-hex SHA")
    data_sha = contract["data_provenance"].get("sha256")
    if not isinstance(data_sha, str) or not SHA256_RE.fullmatch(data_sha):
        raise ValueError("data_provenance.sha256 must be a 64-hex SHA-256")
    for component, specification in contract["execution_cost_model"].items():
        if (
            not isinstance(specification, Mapping)
            or specification.get("basis") not in {"measured", "assumed"}
        ):
            raise ValueError(
                f"execution_cost_model.{component} must identify basis as measured or assumed"
            )


@dataclass(frozen=True)
class ExperimentAudit:
    verdict: Verdict
    prospective_contract_valid: bool
    fingerprint: str | None
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "prospective_contract_valid": self.prospective_contract_valid,
            "fingerprint": self.fingerprint,
            "blockers": list(self.blockers),
        }


def _instant(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _window(value: object, field: str) -> tuple[datetime, datetime]:
    if not isinstance(value, Mapping) or set(value) != {"start", "end"}:
        raise ValueError(f"{field} must contain exactly start and end")
    start = _instant(value["start"], f"{field}.start")
    end = _instant(value["end"], f"{field}.end")
    if start >= end:
        raise ValueError(f"{field} start must precede end")
    return start, end


def contract_fingerprint(contract: Mapping[str, Any]) -> str:
    """Fingerprint every frozen decision, including original code/data provenance."""
    missing = [field for field in CONTRACT_FIELDS if field not in contract]
    extra = sorted(set(contract) - set(CONTRACT_FIELDS))
    if missing or extra:
        raise ValueError(f"contract fields invalid; missing={missing}, extra={extra}")
    _validate_contract_content(contract)
    canonical = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def lock_contract_in_registry(
    registry: Mapping[str, Any], contract: Mapping[str, Any]
) -> dict[str, Any]:
    """Return an updated registry with an immutable lock for a new experiment version.

    Existing legacy records remain readable but cannot be retroactively converted into
    a frozen record. Reusing an already-locked experiment ID with a changed contract is
    rejected; callers must create a new experiment/version and untouched window.
    """
    fingerprint = contract_fingerprint(contract)
    experiment_id = str(contract["experiment_id"])
    rows = registry.get("experiments", [])
    if not isinstance(rows, list):
        raise ValueError("registry.experiments must be a list")
    for row in rows:
        if not isinstance(row, Mapping) or str(row.get("id")) != experiment_id:
            continue
        locked = row.get("locked_contract_fingerprint")
        if not isinstance(locked, str):
            raise ValueError(
                "legacy experiment IDs cannot be retroactively frozen; create a new version"
            )
        if locked != fingerprint:
            raise ValueError("experiment contract is already locked to a different fingerprint")
        stored = row.get("frozen_contract")
        if not isinstance(stored, Mapping) or contract_fingerprint(stored) != fingerprint:
            raise ValueError("registry lock/frozen_contract mismatch")
        return deepcopy(dict(registry))

    updated = deepcopy(dict(registry))
    updated["schema_version"] = max(2, int(updated.get("schema_version", 1)))
    updated["updated_at"] = str(contract["frozen_at"])
    updated_rows = deepcopy(rows)
    updated_rows.append(
        {
            "id": experiment_id,
            "lane": contract["lane"],
            "strategy_id": contract["strategy_id"],
            "status": "FROZEN",
            "evidence_level": "FROZEN_NOT_YET_PROSPECTIVE",
            "locked_contract_fingerprint": fingerprint,
            "frozen_contract": deepcopy(dict(contract)),
            "updated_at": contract["frozen_at"],
        }
    )
    updated["experiments"] = updated_rows
    return updated


def _registry_lock(
    registry: Mapping[str, Any] | None, experiment_id: str
) -> tuple[str | None, Mapping[str, Any] | None, str | None]:
    if registry is None:
        return None, None, "REGISTRY_LOCK_MISSING"
    rows = registry.get("experiments")
    if not isinstance(rows, list):
        return None, None, "MALFORMED_EXPERIMENT_REGISTRY"
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping) and str(row.get("id")) == experiment_id
    ]
    if len(matches) != 1:
        return (
            None,
            None,
            "REGISTRY_LOCK_MISSING" if not matches else "DUPLICATE_REGISTRY_RECORD",
        )
    row = matches[0]
    locked = row.get("locked_contract_fingerprint")
    stored = row.get("frozen_contract")
    if not isinstance(locked, str) or not isinstance(stored, Mapping):
        return None, row, "LEGACY_REGISTRY_RECORD_NOT_LOCKED"
    try:
        stored_fingerprint = contract_fingerprint(stored)
    except (KeyError, ValueError, TypeError):
        return None, row, "MALFORMED_REGISTRY_FROZEN_CONTRACT"
    if stored_fingerprint != locked:
        return None, row, "REGISTRY_LOCK_CORRUPTED"
    return locked, row, None


def validate_evidence(
    report: Mapping[str, Any], *, registry: Mapping[str, Any] | None = None
) -> ExperimentAudit:
    """Classify evidence against both its contract and the independent registry lock."""
    contract = report.get("frozen_contract")
    if contract is None:
        return ExperimentAudit("EXPLORATORY_UNFROZEN", False, None, ("NO_FROZEN_CONTRACT",))
    if not isinstance(contract, Mapping):
        return ExperimentAudit(
            "INVALID_CONTRACT_DRIFT_OR_EVIDENCE_LEAKAGE", False, None, ("MALFORMED_CONTRACT",)
        )
    try:
        actual = contract_fingerprint(contract)
        frozen_at = _instant(contract["frozen_at"], "frozen_at")
        discovery_start, discovery_end = _window(contract["discovery_window"], "discovery_window")
        prospective_start = _instant(contract["prospective_start"], "prospective_start")
        experiment_id = str(contract["experiment_id"])
    except (KeyError, ValueError, TypeError) as exc:
        return ExperimentAudit(
            "INVALID_CONTRACT_DRIFT_OR_EVIDENCE_LEAKAGE",
            False,
            None,
            (f"MALFORMED_CONTRACT: {exc}",),
        )

    blockers: list[str] = []
    if report.get("contract_fingerprint") != actual:
        blockers.append("CONTRACT_FINGERPRINT_MISMATCH")
    locked, _, registry_error = _registry_lock(registry, experiment_id)
    if registry_error:
        blockers.append(registry_error)
    elif locked != actual:
        blockers.append("LOCKED_CONTRACT_DRIFT")
    if not (discovery_start < discovery_end <= prospective_start):
        blockers.append("HOLDOUT_OVERLAPS_DISCOVERY")
    if frozen_at >= prospective_start:
        blockers.append("PROSPECTIVE_WINDOW_BEGAN_BEFORE_FREEZE")

    evaluations = report.get("evaluations", [])
    if not isinstance(evaluations, list):
        blockers.append("MALFORMED_EVALUATIONS")
        evaluations = []
    revisions = report.get("implementation_revisions", [])
    code_provenance = contract.get("code_provenance")
    if isinstance(code_provenance, Mapping):
        repair_shas = {code_provenance.get("commit_sha")}
    else:
        repair_shas = set()
        blockers.append("MALFORMED_CODE_PROVENANCE")
    if not isinstance(revisions, list):
        blockers.append("MALFORMED_IMPLEMENTATION_REVISIONS")
        revisions = []
    else:
        seen_revision_shas: set[str] = set()
        for revision in revisions:
            if (
                not isinstance(revision, Mapping)
                or revision.get("change_type") != "IMPLEMENTATION_REPAIR"
            ):
                blockers.append("NON_REPAIR_CODE_DRIFT")
                continue
            sha = revision.get("commit_sha")
            if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
                blockers.append("MALFORMED_REPAIR_PROVENANCE")
            elif sha in seen_revision_shas:
                blockers.append("DUPLICATE_REPAIR_PROVENANCE")
            else:
                seen_revision_shas.add(sha)
                repair_shas.add(sha)

    latest_revision = revisions[-1] if revisions else None
    latest_code_sha = (
        latest_revision.get("commit_sha") if isinstance(latest_revision, Mapping) else None
    )

    for evaluation in evaluations:
        try:
            start, end = _window(evaluation["window"], "evaluation.window")
            evaluated_at = _instant(evaluation["evaluated_at"], "evaluation.evaluated_at")
            if start < prospective_start or start < discovery_end:
                blockers.append("SAME_WINDOW_SELECTION_EVALUATION_LEAKAGE")
            if frozen_at >= start or frozen_at > evaluated_at:
                blockers.append("EVALUATION_BEFORE_FREEZE")
            if evaluation.get("code_sha") not in repair_shas:
                blockers.append("UNRECORDED_CODE_PROVENANCE")
            data_sha = evaluation.get("data_sha256")
            if not isinstance(data_sha, str) or not SHA256_RE.fullmatch(data_sha):
                blockers.append("EVALUATION_DATA_PROVENANCE_MISSING")
            if latest_code_sha is not None and evaluation.get("code_sha") != latest_code_sha:
                blockers.append("EVIDENCE_NOT_RERUN_AFTER_REPAIR")
            if evaluated_at < end:
                blockers.append("EVALUATED_BEFORE_WINDOW_ENDED")
        except (KeyError, ValueError, TypeError) as exc:
            blockers.append(f"MALFORMED_EVALUATION: {exc}")

    blockers = list(dict.fromkeys(blockers))
    if blockers:
        return ExperimentAudit(
            "INVALID_CONTRACT_DRIFT_OR_EVIDENCE_LEAKAGE",
            False,
            actual,
            tuple(blockers),
        )
    if not evaluations:
        return ExperimentAudit("FROZEN_NOT_YET_PROSPECTIVE", False, actual, ())
    return ExperimentAudit("PROSPECTIVE_CONTRACT_VALID", True, actual, ())


def audit_promotion_report(
    report: Mapping[str, Any],
    *,
    registry: Mapping[str, Any] | None = None,
    economic_evidence_artifact: object = None,
) -> dict[str, Any]:
    """Combine structural validity with independently reproducible economic evidence."""
    output = dict(report)
    audit = validate_evidence(report, registry=registry)
    output["experiment_audit"] = audit.to_dict()
    evaluations = report.get("evaluations")
    latest = (
        evaluations[-1]
        if isinstance(evaluations, list)
        and evaluations
        and isinstance(evaluations[-1], Mapping)
        else None
    )
    payload, economic_blockers = verify_economic_evidence_artifact(
        economic_evidence_artifact,
        contract_fingerprint=audit.fingerprint,
        code_sha=latest.get("code_sha") if latest else None,
        data_sha256=latest.get("data_sha256") if latest else None,
        evaluation_window=latest.get("window") if latest else None,
        required_predicates=ECONOMIC_GATE_CHECKS,
    )
    blockers = list(economic_blockers)
    if not audit.prospective_contract_valid:
        blockers.insert(0, "PROSPECTIVE_CONTRACT_INVALID")
    promotion_ready = audit.prospective_contract_valid and not blockers
    output["promotion_eligible"] = promotion_ready
    output["promotion_gate"] = {
        "promotion_eligible": promotion_ready,
        "blockers": blockers,
        "economic_evidence": (
            {
                "artifact_version": payload.get("artifact_version"),
                "auditor": payload.get("auditor"),
                "policy": payload.get("policy"),
                "evidence_sha256": economic_evidence_artifact.evidence_sha256,
            }
            if payload is not None
            else None
        ),
    }
    return output
