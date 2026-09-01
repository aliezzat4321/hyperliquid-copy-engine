#!/usr/bin/env python3
"""Validate and render the Issue #141 three-lane profitability scoreboard."""
from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ai_team_contract import EVIDENCE_LEVELS, MAX_SNAPSHOT_AGE_HOURS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/ai-team/profitability_scoreboard.json"
OUTPUT = ROOT / "docs/ai-team/PROFITABILITY_SCOREBOARD.md"
LANES = {"lane_1", "lane_2", "lane_3"}


def _required(value: Any, fields: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    missing = sorted(fields - set(value))
    if missing:
        raise ValueError(f"{where} missing fields: {missing}")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string")
    return value


def _timestamp(value: Any, where: str, now: datetime) -> datetime:
    value = _text(value, where)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{where} must be a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{where} must include a timezone")
    parsed = parsed.astimezone(UTC)
    if parsed > now:
        raise ValueError(f"{where} must not be in the future")
    return parsed


def validate(data: dict[str, Any], *, now: datetime | None = None) -> None:
    """Fail closed before render accesses any required scoreboard value."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    top = _required(data, {
        "schema_version", "as_of", "policy_version", "live_trading_enabled",
        "ranking_basis", "lanes", "priority_candidates", "promotion_demotion", "decision",
    }, "scoreboard")
    if top["schema_version"] != 1:
        raise ValueError("scoreboard schema_version must be 1")
    if top["policy_version"] != "quant-promotion-policy-v1":
        raise ValueError("scoreboard must bind the current promotion policy")
    if top["live_trading_enabled"] is not False:
        raise ValueError("scoreboard must not enable live trading")
    as_of = _timestamp(top["as_of"], "scoreboard.as_of", now)
    age_hours = (now - as_of).total_seconds() / 3600
    if age_hours > MAX_SNAPSHOT_AGE_HOURS:
        raise ValueError(
            f"scoreboard.as_of is stale ({age_hours:.1f}h old; "
            f"maximum {MAX_SNAPSHOT_AGE_HOURS}h)"
        )
    _text(top["ranking_basis"], "scoreboard.ranking_basis")
    _text(top["decision"], "scoreboard.decision")

    rows = top["lanes"]
    if not isinstance(rows, list) or len(rows) != 3:
        raise ValueError("scoreboard must contain exactly three lanes")
    row_fields = {
        "rank", "lane", "name", "evidence_level", "promotion_verdict",
        "gross_theoretical_edge", "net_executable_edge", "sample", "outcomes",
        "execution", "stability", "capacity", "engineering_distance",
        "smallest_next_measurement", "sources",
    }
    checked = [_required(row, row_fields, f"lanes[{i}]") for i, row in enumerate(rows)]
    for i, row in enumerate(checked):
        _text(row["lane"], f"lanes[{i}].lane")
        if isinstance(row["rank"], bool) or not isinstance(row["rank"], int):
            raise ValueError(f"lanes[{i}].rank must be an integer")
    if {row["lane"] for row in checked} != LANES:
        raise ValueError("scoreboard must contain lane_1, lane_2, and lane_3 exactly once")
    if sorted(row["rank"] for row in checked) != [1, 2, 3]:
        raise ValueError("scoreboard ranks must be exactly 1, 2, and 3")
    nested = {
        "gross_theoretical_edge": {
            "pnl_usd", "return_pct", "average_pnl_per_closed_trade_usd", "basis",
        },
        "net_executable_edge": {"pnl_usd", "status", "reason"},
        "sample": {
            "eligible_signals", "shadow_opens", "closed_trades", "unresolved_positions",
        },
        "outcomes": {
            "win_rate_pct", "payoff_ratio", "max_drawdown_usd", "downside_concentration",
        },
        "execution": {
            "fees", "slippage_spread_impact", "funding", "copyability_basis",
            "observed_median_signal_latency_ms", "signal_to_shadow_open_rate_pct",
        },
        "stability": {
            "recency", "degradation", "out_of_sample_status", "statistical_confidence",
        },
    }
    for i, row in enumerate(checked):
        where = f"lanes[{i}]"
        _text(row["name"], f"{where}.name")
        evidence_level = _text(row["evidence_level"], f"{where}.evidence_level")
        if evidence_level not in EVIDENCE_LEVELS:
            raise ValueError(
                f"{where}.evidence_level must be one of {sorted(EVIDENCE_LEVELS)}"
            )
        for field in (
            "promotion_verdict", "capacity", "engineering_distance",
            "smallest_next_measurement",
        ):
            _text(row[field], f"{where}.{field}")
        for field, fields in nested.items():
            _required(row[field], fields, f"{where}.{field}")
        sources = row["sources"]
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"{where}.sources must be a non-empty list")
        for j, source in enumerate(sources):
            item = _required(
                source, {"source_type", "source_ref", "observed_at"},
                f"{where}.sources[{j}]",
            )
            _text(item["source_type"], f"{where}.sources[{j}].source_type")
            _text(item["source_ref"], f"{where}.sources[{j}].source_ref")
            _timestamp(item["observed_at"], f"{where}.sources[{j}].observed_at", now)

    candidates = top["priority_candidates"]
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("scoreboard.priority_candidates must be a non-empty list")
    for i, candidate in enumerate(candidates):
        item = _required(
            candidate, {"priority", "candidate", "why", "status"},
            f"priority_candidates[{i}]",
        )
        for field in ("candidate", "why", "status"):
            _text(item[field], f"priority_candidates[{i}].{field}")
    promotion = _required(
        top["promotion_demotion"], {"promotion", "demotion", "live_boundary"},
        "scoreboard.promotion_demotion",
    )
    for field in ("promotion", "demotion", "live_boundary"):
        _text(promotion[field], f"scoreboard.promotion_demotion.{field}")


def _v(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        value = f"{value:.6f}".rstrip("0").rstrip(".")
    return f"{value}{suffix}"


def render(data: dict[str, Any], *, now: datetime | None = None) -> str:
    validate(data, now=now)
    lines = [
        "# Three-Lane Profitability Scoreboard", "",
        "Generated from `docs/ai-team/profitability_scoreboard.json`. Do not hand-edit.", "",
        f"**As of:** {data['as_of']}  ", f"**Policy:** `{data['policy_version']}`  ",
        "**Real trading:** DISABLED", "", f"> {data['ranking_basis']}", "",
        "| Rank | Lane | Gross theoretical edge | Net executable edge | Sample | "
        "Copyability / latency | Verdict | Distance |",
        "|---:|---|---|---|---|---|---|---|",
    ]
    for row in sorted(data["lanes"], key=lambda item: item["rank"]):
        gross, net = row["gross_theoretical_edge"], row["net_executable_edge"]
        sample, execution = row["sample"], row["execution"]
        gross_text = (
            f"{_v(gross['pnl_usd'], ' USD')}; avg "
            f"{_v(gross['average_pnl_per_closed_trade_usd'], ' USD/close')}"
        )
        net_text = f"{net['status']}: {_v(net['pnl_usd'], ' USD')}"
        sample_text = (
            f"{_v(sample.get('closed_trades'))} closes; "
            f"{_v(sample.get('unresolved_positions'))} unresolved"
        )
        execution_text = (
            f"{_v(execution['signal_to_shadow_open_rate_pct'], '%')}; median "
            f"{_v(execution['observed_median_signal_latency_ms'], ' ms')}"
        )
        lines.append(
            f"| {row['rank']} | {row['name']} | {gross_text} | {net_text} | "
            f"{sample_text} | {execution_text} | `{row['promotion_verdict']}` | "
            f"{row['engineering_distance']} |"
        )
    lines += ["", "## Interpretation", "", (
        "Lane 3 ranks first for research effort because it alone has observed shadow closes "
        "and gross dollar PnL, not because executable profitability has been established. "
        "Lane 1 ranks second because frozen wallet-by-asset targets exist but their required "
        "economics are not in trusted evidence. Lane 2 ranks third because zero verified "
        "identities makes an execution comparison impossible."
    ), ""]
    for row in sorted(data["lanes"], key=lambda item: item["rank"]):
        gross, net, sample = (
            row["gross_theoretical_edge"], row["net_executable_edge"], row["sample"],
        )
        outcomes, execution, stability = (
            row["outcomes"], row["execution"], row["stability"],
        )
        lines += [
            f"## {row['rank']}. {row['name']}", "",
            f"- Evidence: `{row['evidence_level']}`; verdict: `{row['promotion_verdict']}`.",
            f"- Gross theoretical: {_v(gross['pnl_usd'], ' USD')} PnL; "
            f"{_v(gross['return_pct'], '%')} return; "
            f"{_v(gross['average_pnl_per_closed_trade_usd'], ' USD')} average PnL per close. "
            f"{gross['basis']}",
            f"- Net executable: {net['status']} — {net['reason']}",
            f"- Sample: {_v(sample.get('eligible_signals'))} eligible signals; "
            f"{_v(sample.get('shadow_opens'))} opens; {_v(sample.get('closed_trades'))} closes; "
            f"{_v(sample.get('unresolved_positions'))} unresolved; "
            f"{_v(sample.get('distinct_days'))} distinct days.",
            f"- Outcomes/risk: win rate {_v(outcomes['win_rate_pct'], '%')}; payoff ratio "
            f"{_v(outcomes['payoff_ratio'])}; max drawdown "
            f"{_v(outcomes['max_drawdown_usd'], ' USD')}; downside concentration "
            f"{_v(outcomes['downside_concentration'])}.",
            f"- Costs/latency/copyability: {execution['fees']}; "
            f"{execution['slippage_spread_impact']}; funding {execution['funding']}; "
            f"{execution['copyability_basis']}.",
            f"- Stability/confidence: {stability['recency']}; degradation "
            f"{stability['degradation']}; {stability['out_of_sample_status']}; "
            f"{stability['statistical_confidence']}.",
            f"- Capacity/concentration: {row['capacity']}.",
            f"- Smallest next measurement: {row['smallest_next_measurement']}",
            "- Sources: " + ", ".join(
                f"{source['source_type']} `{source['source_ref']}` observed {source['observed_at']}"
                for source in row["sources"]
            ) + ".", "",
        ]
    lines += ["## Candidate work order", ""]
    for candidate in data["priority_candidates"]:
        lines.append(
            f"{candidate['priority']}. **{candidate['candidate']}** — {candidate['why']} "
            f"Status: `{candidate['status']}`."
        )
    lines += [
        "", "## Promotion and demotion", "", data["promotion_demotion"]["promotion"], "",
        data["promotion_demotion"]["demotion"], "",
        f"**Safety:** {data['promotion_demotion']['live_boundary']}", "",
        f"**Decision:** {data['decision']}", "",
    ]
    return "\n".join(lines)


def main() -> None:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    OUTPUT.write_text(render(data), encoding="utf-8")


if __name__ == "__main__":
    main()
