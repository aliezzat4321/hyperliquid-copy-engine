from datetime import UTC, datetime

from hlcopy.profitability.three_lane_diagnostics import build_diagnostics, render_markdown


def test_missing_costs_never_become_zero_or_profitability_pass() -> None:
    payload = build_diagnostics(
        lane1={"targets": []},
        lane2={"identifier": {"attempted": 4, "verified": 2}},
        lane2_shadow={"status": "ENOSPC_REGISTRY_WRITE"},
        lane3={
            "reconciliation": {"opens_recorded": 2},
            "slices": [{
                "slice_id": "aggregate", "n_closed": 1, "n_open_unresolved": 1,
                "n_quarantined": 0, "gross_mid_to_mid_pnl_usd": 12,
                "fees_usd": 1, "funding_usd": None, "net_pnl_usd": None,
                "crossing_usd": {"half_spread": None, "impact": None},
                "cost_completeness": "SCENARIO_ONLY", "scenarios": [{"round_trip_bps": 15}],
            }],
        },
        generated_at=datetime(2026, 9, 12, tzinfo=UTC),
    )
    assert payload["profitability_verdict"] == "DIAGNOSTIC_ONLY"
    assert payload["real_trading_enabled"] is False
    assert payload["lanes"][2]["economics"]["net_executable_pnl_usd"] is None
    assert payload["lanes"][2]["completeness"]["net_executable_pnl"] == "BLOCKED"
    assert payload["lanes"][1]["identity_funnel"]["profitability_inference_allowed"] is False
    assert payload["lanes"][1]["rerun_trigger"]["required"] is True


def test_lane1_sparse_is_distinguished_from_negative_economics() -> None:
    payload = build_diagnostics(
        lane1={"targets": [{
            "wallet_address": "0xabc", "coin": "ETH", "event_count": 3,
            "scenarios": [{"realized_actions": 2, "closed_net_pnl_usd": "-4.2"}],
        }]},
        lane2=None,
        lane2_shadow=None,
        lane3=None,
    )
    cohort = payload["lanes"][0]["cohorts"][0]
    assert cohort["net_executable_pnl_usd"] == -4.2
    assert cohort["classification"] == "INSUFFICIENT_SAMPLE"
    assert cohort["evidence_sufficiency"] == "SPARSE"


def test_human_report_carries_required_verdict() -> None:
    payload = build_diagnostics(lane1=None, lane2=None, lane2_shadow=None, lane3=None)
    report = render_markdown(payload)
    assert "PROFITABILITY_VERDICT=DIAGNOSTIC_ONLY" in report
    assert "Real trading remains disabled" in report
