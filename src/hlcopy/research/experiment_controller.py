"""Deterministic, fail-closed experiment freeze and evidence validation.

The controller deliberately operates on plain JSON-compatible mappings so every
research lane can attach its verdict to an existing evidence/promotion report.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

Verdict = Literal[
    "EXPLORATORY_UNFROZEN",
    "FROZEN_NOT_YET_PROSPECTIVE",
    "PROSPECTIVE_SHADOW_VALID",
    "INVALID_CONTRACT_DRIFT_OR_EVIDENCE_LEAKAGE",
]

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
    promotion_eligible: bool
    fingerprint: str | None
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "promotion_eligible": self.promotion_eligible,
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
    start, end = _instant(value["start"], f"{field}.start"), _instant(value["end"], f"{field}.end")
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


def validate_evidence(report: Mapping[str, Any]) -> ExperimentAudit:
    """Classify an evidence report against its embedded frozen contract.

    ``implementation_revisions`` is an append-only evidence ledger outside the
    contract. It may change the implementation SHA only when explicitly marked as
    a repair to the frozen behavior; affected evidence must then name that SHA.
    """
    contract = report.get("frozen_contract")
    if contract is None:
        return ExperimentAudit("EXPLORATORY_UNFROZEN", False, None, ("NO_FROZEN_CONTRACT",))
    if not isinstance(contract, Mapping):
        return ExperimentAudit(
            "INVALID_CONTRACT_DRIFT_OR_EVIDENCE_LEAKAGE",
            False,
            None,
            ("MALFORMED_CONTRACT",),
        )
    try:
        actual = contract_fingerprint(contract)
        frozen_at = _instant(contract["frozen_at"], "frozen_at")
        discovery_start, discovery_end = _window(contract["discovery_window"], "discovery_window")
        prospective_start = _instant(contract["prospective_start"], "prospective_start")
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
    if not (discovery_start < discovery_end <= prospective_start):
        blockers.append("HOLDOUT_OVERLAPS_DISCOVERY")
    if frozen_at > prospective_start:
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
    else:
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
            else:
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
            if frozen_at > start or frozen_at > evaluated_at:
                blockers.append("EVALUATION_BEFORE_FREEZE")
            if evaluation.get("code_sha") not in repair_shas:
                blockers.append("UNRECORDED_CODE_PROVENANCE")
            if latest_code_sha is not None and evaluation.get("code_sha") != latest_code_sha:
                blockers.append("EVIDENCE_NOT_RERUN_AFTER_REPAIR")
            if end <= start:
                blockers.append("MALFORMED_EVALUATION_WINDOW")
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
    return ExperimentAudit("PROSPECTIVE_SHADOW_VALID", True, actual, ())


def audit_promotion_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Attach the reusable experiment verdict to an existing promotion report."""
    output = dict(report)
    audit = validate_evidence(report)
    output["experiment_audit"] = audit.to_dict()
    if not audit.promotion_eligible:
        output["promotion_eligible"] = False
    return output
