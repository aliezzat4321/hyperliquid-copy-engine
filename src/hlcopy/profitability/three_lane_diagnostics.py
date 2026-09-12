from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "hlcopy-three-lane-profitability-diagnostics/v1"
VERDICT = "DIAGNOSTIC_ONLY"
CAUSAL_DIMENSIONS = (
    "coin",
    "side",
    "action_family",
    "source",
    "volatility_regime",
    "momentum_regime",
    "liquidity_regime",
    "time_of_day",
    "day_of_week",
    "notional_bucket",
    "latency_bucket",
    "source_recent_performance_state",
)


def _load(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _empty_lane(lane: str, reason: str) -> dict[str, Any]:
    return {
        "lane": lane,
        "evidence_status": "UNAVAILABLE",
        "counts": {
            key: None
            for key in (
                "source_events",
                "decisions",
                "executions",
                "rejects_or_gaps",
                "opens",
                "closes",
                "open_or_unresolved",
            )
        },
        "economics": _economics(),
        "cohorts": [],
        "causal_trends": [],
        "completeness": {
            "fees": "MISSING",
            "spread_slippage_impact": "MISSING",
            "funding": "MISSING",
            "unresolved_exposure": "MISSING",
            "net_executable_pnl": "BLOCKED",
            "blocking_reasons": [reason],
        },
    }


def _economics() -> dict[str, Any]:
    return {
        key: None
        for key in (
            "gross_realized_pnl_usd",
            "unrealized_mtm_usd",
            "fees_usd",
            "spread_slippage_impact_usd",
            "funding_usd",
            "net_executable_pnl_usd",
            "return_per_trade_usd",
            "win_rate",
            "profit_factor",
            "max_drawdown_usd",
            "worst_trade_usd",
            "execution_rate",
            "reject_rate",
            "observation_days",
            "sample_size",
            "cost_breakeven_bps",
        )
    }


def _lane1(report: dict[str, Any] | None) -> dict[str, Any]:
    if report is None:
        return _empty_lane("lane_1", "LANE1_REPORT_MISSING")
    targets = report.get("targets", [])
    targets = targets if isinstance(targets, list) else []
    cohorts = []
    for target in targets:
        scenarios = target.get("scenarios", []) if isinstance(target, dict) else []
        measured = [row for row in scenarios if isinstance(row, dict)]
        net_values = [float(row["closed_net_pnl_usd"]) for row in measured
                      if row.get("closed_net_pnl_usd") is not None]
        actions = [int(row.get("realized_actions") or 0) for row in measured]
        cohorts.append({
            "cohort": f"{target.get('wallet_address', 'unknown')}:{target.get('coin', 'unknown')}",
            "net_executable_pnl_usd": min(net_values) if net_values else None,
            "sample_size": min(actions) if actions else 0,
            "execution_coverage": target.get("execution_coverage"),
            "evidence_sufficiency": "SPARSE" if not actions or min(actions) < 30 else "DEVELOPING",
            "classification": (
                "INSUFFICIENT_SAMPLE" if not actions or min(actions) < 30 else "DIAGNOSTIC"
            ),
        })
    cohorts.sort(key=lambda row: (row["net_executable_pnl_usd"] is not None,
                                  row["net_executable_pnl_usd"] or float("-inf")), reverse=True)
    lane = _empty_lane("lane_1", "COST_COMPONENT_PROVENANCE_NOT_EXPOSED_BY_LANE1_REPORT")
    lane["evidence_status"] = "FROZEN_PROSPECTIVE_SHADOW"
    lane["counts"] |= {
        "source_events": sum(int(row.get("event_count") or 0) for row in targets),
        "decisions": sum(int(row.get("event_count") or 0) for row in targets),
        "closes": sum(min([int(x.get("realized_actions") or 0) for x in row.get("scenarios", [])]
                          or [0]) for row in targets),
    }
    lane["cohorts"] = cohorts
    lane["rerun_trigger"] = None
    return lane


def _lane2(measurement: dict[str, Any] | None, shadow: dict[str, Any] | None) -> dict[str, Any]:
    if measurement is None:
        return _empty_lane("lane_2", "LANE2_RESOLUTION_MEASUREMENT_MISSING")
    identifier = measurement.get("identifier", {})
    identifier = identifier if isinstance(identifier, dict) else {}
    attempted = int(identifier.get("attempted") or 0)
    verified = int(identifier.get("verified") or measurement.get("verified_count") or 0)
    lane = _empty_lane("lane_2", "NO_EXECUTION_PROFITABILITY_EVIDENCE")
    lane["evidence_status"] = "IDENTITY_FUNNEL_ONLY"
    lane["identity_funnel"] = {
        "attempted": attempted,
        "verified": verified,
        "unresolved": identifier.get("unresolved"),
        "errors": identifier.get("errors"),
        "verified_yield": verified / attempted if attempted else None,
        "latency_ms": identifier.get("latency_ms") or measurement.get("latency_ms"),
        "profitability_inference_allowed": False,
    }
    blocker = measurement.get("runtime_blocker")
    shadow_state = (shadow or {}).get("status") or (shadow or {}).get("state")
    enospc = "ENOSPC" in json.dumps([blocker, shadow_state]).upper()
    lane["rerun_trigger"] = {
        "condition": "STORAGE_WRITES_RESTORED_AFTER_ISSUE_90",
        "required": enospc,
        "action": "RERUN_NET_DIAGNOSTICS_AFTER_VERIFIED_WALLET_SHADOW_HANDOFF",
    }
    lane["completeness"]["blocking_reasons"] = [
        "ENOSPC_SHADOW_WRITES" if enospc else "NO_VERIFIED_WALLET_SHADOW_EXECUTIONS"
    ]
    return lane


def _lane3(report: dict[str, Any] | None) -> dict[str, Any]:
    if report is None:
        return _empty_lane("lane_3", "LANE3_NET_EDGE_REPORT_MISSING")
    slices = report.get("slices", [])
    aggregate = next((row for row in slices if row.get("slice_id") == "aggregate"), None)
    if aggregate is None:
        return _empty_lane("lane_3", "LANE3_AGGREGATE_SLICE_MISSING")
    lane = _empty_lane("lane_3", "LANE3_COSTS_INCOMPLETE")
    lane["evidence_status"] = "RETROSPECTIVE_WHOLE_LEDGER"
    recon = report.get("reconciliation", {})
    lane["counts"] |= {
        "source_events": recon.get("opens_recorded"),
        "opens": recon.get("opens_recorded"),
        "closes": aggregate.get("n_closed"),
        "open_or_unresolved": aggregate.get("n_open_unresolved"),
        "rejects_or_gaps": aggregate.get("n_quarantined"),
    }
    econ = lane["economics"]
    econ |= {
        "gross_realized_pnl_usd": aggregate.get("gross_mid_to_mid_pnl_usd"),
        "fees_usd": aggregate.get("fees_usd"),
        "funding_usd": aggregate.get("funding_usd"),
        "net_executable_pnl_usd": aggregate.get("net_pnl_usd"),
        "win_rate": aggregate.get("win_rate_net"),
        "profit_factor": aggregate.get("profit_factor_net"),
        "max_drawdown_usd": aggregate.get("max_drawdown_usd"),
        "observation_days": aggregate.get("distinct_utc_days"),
        "sample_size": aggregate.get("n_closed"),
        "cost_breakeven_bps": aggregate.get("breakeven_cost_bps_notional_weighted"),
    }
    closed = int(aggregate.get("n_closed") or 0)
    quarantined = int(aggregate.get("n_quarantined") or 0)
    gross = econ["gross_realized_pnl_usd"]
    econ["return_per_trade_usd"] = gross / closed if gross is not None and closed else None
    capacity = aggregate.get("capacity", {})
    econ["execution_rate"] = capacity.get("legs_complete_share")
    denominator = closed + quarantined
    econ["reject_rate"] = quarantined / denominator if denominator else None
    crossing = aggregate.get("crossing_usd", {})
    values = [crossing.get("half_spread"), crossing.get("impact")]
    econ["spread_slippage_impact_usd"] = sum(values) if all(v is not None for v in values) else None
    complete = aggregate.get("cost_completeness") == "MEASURED"
    lane["completeness"] = {
        "fees": "MEASURED" if aggregate.get("fees_usd") is not None else "MISSING",
        "spread_slippage_impact": "MEASURED" if complete else "MISSING_OR_SCENARIO",
        "funding": "MEASURED" if aggregate.get("funding_usd") is not None else "MISSING",
        "unresolved_exposure": "COUNT_ONLY" if aggregate.get("unresolved") else "MISSING",
        "net_executable_pnl": "MEASURED" if complete else "BLOCKED",
        "blocking_reasons": [] if complete else ["ONE_OR_MORE_COST_COMPONENTS_UNMEASURED"],
    }
    lane["cohorts"] = [row for row in slices if row.get("slice_id") != "aggregate"]
    lane["scenario_bands"] = aggregate.get("scenarios", [])
    lane["causal_trends"] = report.get("diagnostics_non_promotable", [])
    return lane


def build_diagnostics(*, lane1: dict[str, Any] | None, lane2: dict[str, Any] | None,
                      lane2_shadow: dict[str, Any] | None, lane3: dict[str, Any] | None,
                      generated_at: datetime | None = None) -> dict[str, Any]:
    lanes = [_lane1(lane1), _lane2(lane2, lane2_shadow), _lane3(lane3)]
    gaps = sorted({reason for lane in lanes for reason in lane["completeness"]["blocking_reasons"]})
    return {
        "schema": SCHEMA,
        "generated_at": (generated_at or datetime.now(UTC)).isoformat(),
        "scope": "HYPERLIQUID_ONLY",
        "real_trading_enabled": False,
        "profitability_verdict": VERDICT,
        "lanes": lanes,
        "causal_slice_contract": {
            "allowed_decision_time_dimensions": list(CAUSAL_DIMENSIONS),
            "label": "EXPLORATORY_ONLY",
            "post_outcome_filters_forbidden": True,
        },
        "evidence_gaps": gaps,
        "candidate_frozen_experiments": [
            {
                "status": "CANDIDATE_NOT_REGISTERED",
                "lane": "lane_1",
                "hypothesis": (
                    "Higher decision-time depth and lower spread improve wallet-coin net outcomes"
                ),
                "required_freeze": (
                    "Register dimensions, cutoffs, universe and UTC-day clustered statistic "
                    "under #197"
                ),
            },
            {
                "status": "CANDIDATE_NOT_REGISTERED",
                "lane": "lane_3",
                "hypothesis": "Lower observed signal-to-arrival latency improves net outcomes",
                "required_freeze": "Register latency buckets and prospective start under #197",
            },
        ],
        "next_step": "Register any selected candidate under issue #197 before prospective use.",
        "gates": {"issue_273_pass_required": True, "prospective_evidence_required": True},
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = ["# Three-lane net profitability diagnostics", "",
             f"`PROFITABILITY_VERDICT={payload['profitability_verdict']}`", "",
             "| Lane | Evidence | Gross USD | Net executable USD | Closed | Open/unresolved |",
             "|---|---|---:|---:|---:|---:|"]
    for lane in payload["lanes"]:
        e, c = lane["economics"], lane["counts"]
        def show(value: Any) -> str:
            return "—" if value is None else str(value)
        lines.append(f"| {lane['lane']} | {lane['evidence_status']} | "
                     f"{show(e['gross_realized_pnl_usd'])} | {show(e['net_executable_pnl_usd'])} | "
                     f"{show(c['closes'])} | {show(c['open_or_unresolved'])} |")
    lines += ["", "## Completeness", "",
              "| Lane | Fees | Spread/slippage/impact | Funding | Net | Blockers |",
              "|---|---|---|---|---|---|"]
    for lane in payload["lanes"]:
        c = lane["completeness"]
        lines.append(f"| {lane['lane']} | {c['fees']} | {c['spread_slippage_impact']} | "
                     f"{c['funding']} | {c['net_executable_pnl']} | "
                     f"{', '.join(c['blocking_reasons']) or 'none'} |")
    lines += [
        "",
        "All causal slices are exploratory and decision-time-only. Candidate filters must be "
        "frozen under #197. Identity quality is not profitability evidence. #273 and prospective "
        "evidence gates remain mandatory. Real trading remains disabled.",
        "",
    ]
    return "\n".join(lines)


def write_diagnostics(*, lane1_path: Path | None, lane2_path: Path | None,
                      lane2_shadow_path: Path | None, lane3_path: Path | None,
                      json_output: Path, markdown_output: Path) -> dict[str, Any]:
    payload = build_diagnostics(lane1=_load(lane1_path), lane2=_load(lane2_path),
                                lane2_shadow=_load(lane2_shadow_path), lane3=_load(lane3_path))
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_output.write_text(render_markdown(payload), encoding="utf-8")
    return payload
