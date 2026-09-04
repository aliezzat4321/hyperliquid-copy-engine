from __future__ import annotations

from copy import deepcopy

import pytest

from hlcopy.profitability.evidence_auditor import audit_evidence, lane3_bundle


def _cost(amount: str = "1", basis: str = "measured") -> dict[str, str]:
    return {"amount": amount, "basis": basis}


def _valid_bundle() -> dict[str, object]:
    return {
        "report_version": "lane-3-net-edge-v1",
        "policy_version": "v1",
        "audited_at": "2026-01-04T00:00:00Z",
        "max_data_age_seconds": 172800,
        "provenance": {
            "source": "synthetic:test-ledger",
            "data_sha256": "a" * 64,
            "code_commit": "0" * 40,
        },
        "evaluation_window": {
            "start": "2026-01-02T00:00:00Z",
            "end": "2026-01-03T00:00:00Z",
        },
        "selection": {
            "prospective": True,
            "frozen_at": "2026-01-01T00:00:00Z",
        },
        "funding_applicable": True,
        "population": {"input_count": 1},
        "positions": [
            {
                "position_id": "p1",
                "status": "closed",
                "timestamps": {
                    "signal": "2026-01-02T00:00:00Z",
                    "decision": "2026-01-02T00:00:01Z",
                    "shadow_or_execution": "2026-01-02T00:00:02Z",
                    "open": "2026-01-02T00:00:03Z",
                    "close": "2026-01-03T00:00:00Z",
                },
                "economics": {
                    "gross_pnl": "21",
                    "fees": _cost(),
                    "spread": _cost(),
                    "depth": _cost(),
                    "slippage": _cost(),
                    "impact": _cost(),
                    "funding": {"amount": "1", "coverage": "complete"},
                },
            }
        ],
        "economics_totals": {"final_net": "15"},
    }


def _codes(report: dict[str, object]) -> set[str]:
    return {item["code"] for item in report["blockers"]}


def test_complete_valid_ledger_reconciles_and_passes() -> None:
    report = audit_evidence(_valid_bundle())
    assert report["status"] == "PASS"
    assert report["promotion_eligible"] is True
    assert report["validated_profitability_allowed"] is True
    assert report["economics_state"] == "RECONCILED_POSITIVE_ECONOMICS"
    assert report["economics_basis"] == "MEASURED_COMPONENTS"
    assert report["economics"]["final_net"] == "15"


@pytest.mark.parametrize(
    ("mutation", "blocker"),
    [
        (lambda b: b["positions"][0]["timestamps"].pop("close"), "MALFORMED_CLOSE"),
        (lambda b: b["positions"][0]["economics"].pop("gross_pnl"), "MALFORMED_CLOSE"),
        (lambda b: b["positions"].append(deepcopy(b["positions"][0])), "DUPLICATE_POSITIONS"),
        (lambda b: b["positions"][0].update(orphan=True), "ORPHAN_ROWS"),
        (lambda b: b["positions"][0]["economics"].pop("fees"), "FEES_EVIDENCE_MISSING"),
        (lambda b: b["positions"][0]["economics"].pop("funding"), "FUNDING_COVERAGE_MISSING"),
        (lambda b: b.update(audited_at="2026-01-10T00:00:00Z"), "STALE_DATA"),
        (lambda b: b["selection"].update(frozen_at="2026-01-02T00:00:00Z"), "SAME_WINDOW_LEAKAGE"),
        (lambda b: b["economics_totals"].update(final_net="16"), "PNL_UNRECONCILED"),
        (lambda b: b["provenance"].update(code_commit="0123456789abcdef"), "CODE_COMMIT_INVALID"),
    ],
)
def test_required_integrity_failures_are_precise(mutation, blocker: str) -> None:
    bundle = _valid_bundle()
    mutation(bundle)
    if blocker == "DUPLICATE_POSITIONS":
        bundle["population"]["input_count"] = 2
    report = audit_evidence(bundle)
    assert report["status"] == "FAIL"
    assert report["promotion_eligible"] is False
    assert blocker in _codes(report)


def test_non_prospective_evidence_can_be_diagnostic_but_not_promotable() -> None:
    bundle = _valid_bundle()
    bundle["selection"] = {"prospective": False}
    report = audit_evidence(bundle)
    assert report["status"] == "PASS"
    assert report["economics_state"] == "RECONCILED_POSITIVE_ECONOMICS"
    assert report["promotion_eligible"] is False
    assert report["validated_profitability_allowed"] is False


def test_assumed_material_costs_never_emit_validated_or_promotion_verdict() -> None:
    bundle = _valid_bundle()
    bundle["positions"][0]["economics"]["slippage"] = _cost("1", basis="assumption")
    report = audit_evidence(bundle)
    assert report["status"] == "PASS"
    assert report["economics_basis"] == "SCENARIO_ASSUMPTIONS"
    assert report["assumed_cost_components"] == ["slippage"]
    assert report["promotion_eligible"] is False
    assert report["validated_profitability_allowed"] is False


def test_overlapping_positions_do_not_create_false_global_monotonicity_failure() -> None:
    bundle = _valid_bundle()
    second = deepcopy(bundle["positions"][0])
    second["position_id"] = "p2"
    second["timestamps"] = {
        "signal": "2026-01-02T12:00:00Z",
        "decision": "2026-01-02T12:00:01Z",
        "shadow_or_execution": "2026-01-02T12:00:02Z",
        "open": "2026-01-02T12:00:03Z",
        "close": "2026-01-02T18:00:00Z",
    }
    bundle["positions"].append(second)
    bundle["population"]["input_count"] = 2
    bundle["economics_totals"]["final_net"] = "30"
    report = audit_evidence(bundle)
    assert report["status"] == "PASS"
    assert "LEDGER_NON_MONOTONIC" not in _codes(report)
    assert report["counts"]["closed"] == 2


def test_lane3_normalizer_preserves_independent_population_baseline() -> None:
    rows = [
        {
            "type": "shadow_opened",
            "ts": "2026-01-02T00:00:03Z",
            "signal": {"sourceBaseId": "p1", "sourceTimeMs": 1767312000000},
        }
    ]
    manifest = _valid_bundle()
    del manifest["positions"]
    manifest["population"] = {"input_count": 2}
    manifest["economics_totals"] = {"final_net": "0"}
    report = audit_evidence(lane3_bundle(rows, manifest))
    assert "POPULATION_UNRECONCILED" in _codes(report)


def test_missing_outcome_cannot_disappear_from_closed_and_unresolved() -> None:
    bundle = _valid_bundle()
    bundle["positions"][0]["status"] = ""
    report = audit_evidence(bundle)
    assert "POSITION_DISAPPEARED" in _codes(report)
    assert report["counts"]["missing_outcomes"] == 1


def test_unknown_is_not_converted_to_zero_or_negative() -> None:
    bundle = _valid_bundle()
    del bundle["positions"][0]["economics"]["fees"]
    report = audit_evidence(bundle)
    assert report["economics_state"] == "UNKNOWN_MISSING_EVIDENCE"


def test_actual_negative_economics_is_distinct_and_integrity_passes() -> None:
    bundle = _valid_bundle()
    bundle["positions"][0]["economics"]["gross_pnl"] = "3"
    bundle["economics_totals"]["final_net"] = "-3"
    report = audit_evidence(bundle)
    assert report["status"] == "PASS"
    assert report["economics_state"] == "ZERO_OR_NEGATIVE_ECONOMICS"
    assert report["promotion_eligible"] is False


def test_lane3_incomplete_ledger_allows_diagnostics_but_fails_closed() -> None:
    rows = [
        {
            "type": "shadow_opened",
            "ts": "2026-01-02T00:00:03Z",
            "signal": {"sourceBaseId": "p1", "sourceTimeMs": 1767312000000},
        }
    ]
    manifest = _valid_bundle()
    del manifest["positions"]
    manifest["economics_totals"] = {"final_net": "0"}
    report = audit_evidence(lane3_bundle(rows, manifest))
    assert report["status"] == "FAIL"
    assert report["counts"]["unresolved"] == 1
    assert "UNRESOLVED_MTM_MISSING" in _codes(report)
    assert report["validated_profitability_allowed"] is False
