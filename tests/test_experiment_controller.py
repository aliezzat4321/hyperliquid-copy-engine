from copy import deepcopy

from hlcopy.research.experiment_controller import (
    audit_promotion_report,
    contract_fingerprint,
    validate_evidence,
)

SHA1, SHA2 = "1" * 40, "2" * 40


def contract():
    return {
        "contract_version": 1, "experiment_id": "EXP-197-001",
        "hypothesis": "A point-in-time signal has positive net edge.",
        "causal_rationale": "The signal precedes copied fills.", "lane": "lane_1",
        "strategy_id": "signal-v1", "eligible_universe": {"coins": ["BTC"]},
        "selection_rule": {"score_gte": 2}, "decision_time_features": ["score"],
        "parameters": {"threshold": 2}, "parameter_grid": {"threshold": [1, 2, 3]},
        "discovery_window": {"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
        "prospective_start": "2026-02-02T00:00:00Z",
        "execution_cost_model": {"fees": {"value_bps": 4, "basis": "measured"}},
        "target_notional_capacity": {"notional_usd": 100, "capacity_method": "book walk"},
        "minimum_evidence": {"samples": 30, "distinct_days": 7},
        "primary_metrics": ["net_return_bps"], "uncertainty_method": "day-block bootstrap",
        "multiple_testing_treatment": "Holm correction across three thresholds",
        "success_criteria": {"lower_bound_net_bps_gt": 0},
        "failure_criteria": {"upper_bound_net_bps_lte": 0},
        "abandonment_criteria": "Abandon after 30 days without 30 samples.",
        "code_provenance": {"commit_sha": SHA1, "entrypoint": "example"},
        "data_provenance": {"dataset": "immutable:test", "sha256": "a" * 64},
        "frozen_at": "2026-02-01T12:00:00Z",
    }


def report(with_result=True):
    frozen = contract()
    return {
        "frozen_contract": frozen,
        "contract_fingerprint": contract_fingerprint(frozen),
        "implementation_revisions": [],
        "evaluations": (
            [{
                "window": {
                    "start": "2026-02-02T00:00:00Z",
                    "end": "2026-03-01T00:00:00Z",
                },
                "evaluated_at": "2026-03-02T00:00:00Z",
                "code_sha": SHA1,
            }]
            if with_result
            else []
        ),
    }


def test_unfrozen_and_frozen_without_results_are_not_promotable():
    assert validate_evidence({}).verdict == "EXPLORATORY_UNFROZEN"
    assert validate_evidence(report(False)).verdict == "FROZEN_NOT_YET_PROSPECTIVE"


def test_threshold_parameter_or_window_mutation_after_result_fails():
    for field, value in (
        (("success_criteria", "lower_bound_net_bps_gt"), -1),
        (("parameters", "threshold"), 3),
        (("prospective_start",), "2025-02-02T00:00:00Z"),
    ):
        candidate = report()
        if len(field) == 1:
            candidate["frozen_contract"][field[0]] = value
        else:
            candidate["frozen_contract"][field[0]][field[1]] = value
        assert validate_evidence(candidate).verdict == "INVALID_CONTRACT_DRIFT_OR_EVIDENCE_LEAKAGE"


def test_implementation_only_repair_preserves_contract_and_requires_rerun_sha():
    candidate = report()
    candidate["implementation_revisions"] = [{
        "commit_sha": SHA2,
        "change_type": "IMPLEMENTATION_REPAIR",
        "reason": "Match frozen fee rounding",
    }]
    candidate["evaluations"][0]["code_sha"] = SHA2
    audit = validate_evidence(candidate)
    assert audit.verdict == "PROSPECTIVE_SHADOW_VALID"
    assert audit.fingerprint == candidate["contract_fingerprint"]

    stale = report()
    stale["implementation_revisions"] = candidate["implementation_revisions"]
    assert "EVIDENCE_NOT_RERUN_AFTER_REPAIR" in validate_evidence(stale).blockers


def test_holdout_overlap_evaluation_before_freeze_and_same_window_leakage_fail():
    overlap = report(False)
    overlap["frozen_contract"]["prospective_start"] = "2026-01-15T00:00:00Z"
    overlap["contract_fingerprint"] = contract_fingerprint(overlap["frozen_contract"])
    assert "HOLDOUT_OVERLAPS_DISCOVERY" in validate_evidence(overlap).blockers
    early = report()
    early["frozen_contract"]["frozen_at"] = "2026-02-03T00:00:00Z"
    early["contract_fingerprint"] = contract_fingerprint(early["frozen_contract"])
    assert "EVALUATION_BEFORE_FREEZE" in validate_evidence(early).blockers
    leaked = report()
    leaked["evaluations"][0]["window"]["start"] = "2026-01-15T00:00:00Z"
    assert "SAME_WINDOW_SELECTION_EVALUATION_LEAKAGE" in validate_evidence(leaked).blockers


def test_promotion_report_integration_fails_closed():
    promoted = audit_promotion_report({**report(), "promotion_eligible": True})
    assert promoted["experiment_audit"]["promotion_eligible"] is True
    unfrozen = audit_promotion_report({"promotion_eligible": True})
    assert unfrozen["promotion_eligible"] is False


def test_new_version_gets_new_fingerprint():
    original = report()
    replacement = deepcopy(original["frozen_contract"])
    replacement["experiment_id"] = "EXP-197-002"
    replacement["parameters"]["threshold"] = 3
    replacement["prospective_start"] = "2026-03-02T00:00:00Z"
    assert contract_fingerprint(replacement) != original["contract_fingerprint"]
