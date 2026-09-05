from copy import deepcopy

import pytest

from hlcopy.profitability.evidence_auditor import (
    EconomicEvidenceArtifact,
    build_economic_evidence_artifact,
)
from hlcopy.research.experiment_controller import (
    audit_promotion_report,
    contract_fingerprint,
    lock_contract_in_registry,
    validate_evidence,
)

SHA1, SHA2 = "1" * 40, "2" * 40


def contract():
    return {
        "contract_version": 1,
        "experiment_id": "EXP-197-001",
        "hypothesis": "A point-in-time signal has positive net edge.",
        "causal_rationale": "The signal precedes copied fills.",
        "lane": "lane_1",
        "strategy_id": "signal-v1",
        "eligible_universe": {"coins": ["BTC"]},
        "selection_rule": {"score_gte": 2},
        "decision_time_features": ["score"],
        "parameters": {"threshold": 2},
        "parameter_grid": {"threshold": [1, 2, 3]},
        "discovery_window": {
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-02-01T00:00:00Z",
        },
        "prospective_start": "2026-02-02T00:00:00Z",
        "execution_cost_model": {"fees": {"value_bps": 4, "basis": "measured"}},
        "target_notional_capacity": {"notional_usd": 100, "capacity_method": "book walk"},
        "minimum_evidence": {"samples": 30, "distinct_days": 7},
        "primary_metrics": ["net_return_bps"],
        "uncertainty_method": "day-block bootstrap",
        "multiple_testing_treatment": "Holm correction across three thresholds",
        "success_criteria": {"lower_bound_net_bps_gt": 0},
        "failure_criteria": {"upper_bound_net_bps_lte": 0},
        "abandonment_criteria": "Abandon after 30 days without 30 samples.",
        "code_provenance": {"commit_sha": SHA1, "entrypoint": "example"},
        "data_provenance": {"dataset": "immutable:test", "sha256": "a" * 64},
        "frozen_at": "2026-02-01T12:00:00Z",
    }


def registry_for(frozen=None):
    frozen = frozen or contract()
    return lock_contract_in_registry(
        {"schema_version": 1, "updated_at": "2026-01-01T00:00:00Z", "experiments": []},
        frozen,
    )


def report(with_result=True):
    frozen = contract()
    return {
        "frozen_contract": frozen,
        "contract_fingerprint": contract_fingerprint(frozen),
        "implementation_revisions": [],
        "evaluations": (
            [
                {
                    "window": {
                        "start": "2026-02-02T00:00:00Z",
                        "end": "2026-03-01T00:00:00Z",
                    },
                    "evaluated_at": "2026-03-02T00:00:00Z",
                    "code_sha": SHA1,
                    "data_sha256": "b" * 64,
                }
            ]
            if with_result
            else []
        ),
    }


def test_unfrozen_and_persisted_frozen_without_results_are_not_promotable():
    assert validate_evidence({}).verdict == "EXPLORATORY_UNFROZEN"
    candidate = report(False)
    audit = validate_evidence(candidate, registry=registry_for(candidate["frozen_contract"]))
    assert audit.verdict == "FROZEN_NOT_YET_PROSPECTIVE"
    assert audit.prospective_contract_valid is False


def test_embedded_fingerprint_without_independent_registry_lock_fails_closed():
    audit = validate_evidence(report())
    assert audit.verdict == "INVALID_CONTRACT_DRIFT_OR_EVIDENCE_LEAKAGE"
    assert "REGISTRY_LOCK_MISSING" in audit.blockers


def test_contract_mutation_cannot_bypass_lock_by_recomputing_report_fingerprint():
    original = report()
    registry = registry_for(original["frozen_contract"])
    candidate = deepcopy(original)
    candidate["frozen_contract"]["parameters"]["threshold"] = 3
    candidate["contract_fingerprint"] = contract_fingerprint(candidate["frozen_contract"])
    audit = validate_evidence(candidate, registry=registry)
    assert audit.verdict == "INVALID_CONTRACT_DRIFT_OR_EVIDENCE_LEAKAGE"
    assert "LOCKED_CONTRACT_DRIFT" in audit.blockers


def test_locked_experiment_id_rejects_contract_rewrite_and_requires_new_version():
    frozen = contract()
    registry = registry_for(frozen)
    changed = deepcopy(frozen)
    changed["parameters"]["threshold"] = 3
    with pytest.raises(ValueError, match="already locked"):
        lock_contract_in_registry(registry, changed)

    changed["experiment_id"] = "EXP-197-002"
    changed["prospective_start"] = "2026-03-02T00:00:00Z"
    changed["frozen_at"] = "2026-03-01T12:00:00Z"
    updated = lock_contract_in_registry(registry, changed)
    assert len(updated["experiments"]) == 2


def test_legacy_registry_record_remains_readable_but_cannot_be_retroactively_frozen():
    legacy = {
        "schema_version": 1,
        "experiments": [{"id": "EXP-197-001", "evidence_level": "EXPLORATORY"}],
    }
    with pytest.raises(ValueError, match="legacy experiment IDs"):
        lock_contract_in_registry(legacy, contract())
    audit = validate_evidence(report(), registry=legacy)
    assert "LEGACY_REGISTRY_RECORD_NOT_LOCKED" in audit.blockers


def test_implementation_only_repair_preserves_lock_and_requires_rerun_sha():
    candidate = report()
    registry = registry_for(candidate["frozen_contract"])
    candidate["implementation_revisions"] = [
        {
            "commit_sha": SHA2,
            "change_type": "IMPLEMENTATION_REPAIR",
            "reason": "Match frozen fee rounding",
        }
    ]
    candidate["evaluations"][0]["code_sha"] = SHA2
    audit = validate_evidence(candidate, registry=registry)
    assert audit.verdict == "PROSPECTIVE_CONTRACT_VALID"
    assert audit.fingerprint == candidate["contract_fingerprint"]

    stale = report()
    stale["implementation_revisions"] = candidate["implementation_revisions"]
    assert "EVIDENCE_NOT_RERUN_AFTER_REPAIR" in validate_evidence(
        stale, registry=registry
    ).blockers


def test_holdout_overlap_evaluation_before_freeze_and_same_window_leakage_fail():
    base = report(False)
    registry = registry_for(base["frozen_contract"])

    overlap = deepcopy(base)
    overlap["frozen_contract"]["prospective_start"] = "2026-01-15T00:00:00Z"
    overlap["contract_fingerprint"] = contract_fingerprint(overlap["frozen_contract"])
    audit = validate_evidence(overlap, registry=registry)
    assert "HOLDOUT_OVERLAPS_DISCOVERY" in audit.blockers
    assert "LOCKED_CONTRACT_DRIFT" in audit.blockers

    early = report()
    early["frozen_contract"]["frozen_at"] = "2026-02-03T00:00:00Z"
    early["contract_fingerprint"] = contract_fingerprint(early["frozen_contract"])
    assert "PROSPECTIVE_WINDOW_BEGAN_BEFORE_FREEZE" in validate_evidence(
        early, registry=registry
    ).blockers

    leaked = report()
    leaked["evaluations"][0]["window"]["start"] = "2026-01-15T00:00:00Z"
    assert "SAME_WINDOW_SELECTION_EVALUATION_LEAKAGE" in validate_evidence(
        leaked, registry=registry
    ).blockers


def test_evaluation_timestamp_must_follow_window_end():
    candidate = report()
    candidate["evaluations"][0]["evaluated_at"] = "2026-02-15T00:00:00Z"
    audit = validate_evidence(candidate, registry=registry_for(candidate["frozen_contract"]))
    assert "EVALUATED_BEFORE_WINDOW_ENDED" in audit.blockers


def economic_bundle(candidate):
    evaluation = candidate["evaluations"][-1]
    costs = {
        name: {"amount": "1", "basis": "measured"}
        for name in ("fees", "spread", "depth", "slippage", "impact")
    }
    return {
        "report_version": "test-economic-report-v1",
        "policy_version": "quant-promotion-policy-v2",
        "audited_at": "2026-03-02T00:00:00Z",
        "max_data_age_seconds": 86400,
        "provenance": {
            "source": "immutable:test-evidence",
            "data_sha256": evaluation["data_sha256"],
            "code_commit": evaluation["code_sha"],
        },
        "evaluation_window": evaluation["window"],
        "selection": {"prospective": True, "frozen_at": "2026-02-01T12:00:00Z"},
        "funding_applicable": True,
        "population": {"input_count": 1},
        "positions": [
            {
                "position_id": "p1",
                "status": "closed",
                "timestamps": {
                    "signal": "2026-02-02T00:00:00Z",
                    "decision": "2026-02-02T00:00:01Z",
                    "shadow_or_execution": "2026-02-02T00:00:02Z",
                    "open": "2026-02-02T00:00:03Z",
                    "close": "2026-03-01T00:00:00Z",
                },
                "economics": {
                    "gross_pnl": "11",
                    **costs,
                    "funding": {"amount": "1", "coverage": "complete"},
                },
            },
        ],
        "economics_totals": {"final_net": "5"},
    }


def economic_artifact(candidate, **predicate_overrides):
    predicates = {
        name: True
        for name in (
            "minimum_evidence",
            "primary_metrics",
            "success_criteria",
            "uncertainty_method",
            "multiple_testing_treatment",
            "execution_costs",
            "unresolved_exposure",
            "capacity",
        )
    }
    predicates.update(predicate_overrides)
    return build_economic_evidence_artifact(
        economic_bundle(candidate),
        contract_fingerprint=candidate["contract_fingerprint"],
        predicates=predicates,
    )


def test_promotion_report_integration_requires_bound_economic_artifact():
    candidate = report()
    registry = registry_for(candidate["frozen_contract"])
    promoted = audit_promotion_report(
        {**candidate, "promotion_eligible": True},
        registry=registry,
        economic_evidence_artifact=economic_artifact(candidate),
    )
    assert promoted["experiment_audit"]["prospective_contract_valid"] is True
    assert promoted["promotion_eligible"] is True

    unlocked = audit_promotion_report({**candidate, "promotion_eligible": True})
    assert unlocked["promotion_eligible"] is False
    assert "REGISTRY_LOCK_MISSING" in unlocked["experiment_audit"]["blockers"]

    unfrozen = audit_promotion_report({"promotion_eligible": True}, registry=registry)
    assert unfrozen["promotion_eligible"] is False


def test_fabricated_hash_and_all_true_mapping_fails_closed():
    candidate = report()
    fabricated = {
        "status": "PASS",
        "promotion_eligible": True,
        "evidence_sha256": "c" * 64,
        "checks": {
            name: True
            for name in (
                "minimum_evidence",
                "primary_metrics",
                "success_criteria",
                "uncertainty_method",
                "multiple_testing_treatment",
                "execution_costs",
                "unresolved_exposure",
                "capacity",
            )
        },
    }
    result = audit_promotion_report(
        candidate,
        registry=registry_for(candidate["frozen_contract"]),
        economic_evidence_artifact=fabricated,
    )
    assert result["promotion_eligible"] is False
    assert "DOWNSTREAM_ECONOMIC_ARTIFACT_REQUIRED" in result["promotion_gate"]["blockers"]


@pytest.mark.parametrize(
    "failed_predicate",
    [
        "minimum_evidence",
        "primary_metrics",
        "success_criteria",
        "uncertainty_method",
        "multiple_testing_treatment",
        "execution_costs",
        "unresolved_exposure",
        "capacity",
    ],
)
def test_bound_artifact_requires_every_predicate(failed_predicate):
    candidate = report()
    result = audit_promotion_report(
        candidate,
        registry=registry_for(candidate["frozen_contract"]),
        economic_evidence_artifact=economic_artifact(candidate, **{failed_predicate: False}),
    )
    assert result["promotion_eligible"] is False
    expected = f"ECONOMIC_AUDIT_{failed_predicate.upper()}_NOT_SATISFIED"
    assert expected in result["promotion_gate"]["blockers"]


def test_artifact_payload_tampering_breaks_cryptographic_binding():
    candidate = report()
    artifact = economic_artifact(candidate)
    tampered = EconomicEvidenceArtifact(
        artifact.canonical_payload.replace(b'"final_net":"5"', b'"final_net":"6"'),
        artifact.evidence_sha256,
    )
    result = audit_promotion_report(
        candidate,
        registry=registry_for(candidate["frozen_contract"]),
        economic_evidence_artifact=tampered,
    )
    assert result["promotion_eligible"] is False
    assert "ECONOMIC_ARTIFACT_HASH_MISMATCH" in result["promotion_gate"]["blockers"]


def test_new_version_gets_new_fingerprint():
    original = report()
    replacement = deepcopy(original["frozen_contract"])
    replacement["experiment_id"] = "EXP-197-002"
    replacement["parameters"]["threshold"] = 3
    replacement["prospective_start"] = "2026-03-02T00:00:00Z"
    replacement["frozen_at"] = "2026-03-01T12:00:00Z"
    assert contract_fingerprint(replacement) != original["contract_fingerprint"]
