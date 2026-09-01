#!/usr/bin/env python3
"""Validate and render the Issue #141 three-lane profitability scoreboard."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/ai-team/profitability_scoreboard.json"
OUTPUT = ROOT / "docs/ai-team/PROFITABILITY_SCOREBOARD.md"
LANES = {"lane_1", "lane_2", "lane_3"}


def validate(data: dict[str, Any]) -> None:
    if data.get("schema_version") != 1:
        raise ValueError("scoreboard schema_version must be 1")
    if data.get("policy_version") != "quant-promotion-policy-v1":
        raise ValueError("scoreboard must bind the current promotion policy")
    if data.get("live_trading_enabled") is not False:
        raise ValueError("scoreboard must not enable live trading")
    rows = data.get("lanes")
    if not isinstance(rows, list) or len(rows) != 3:
        raise ValueError("scoreboard must contain exactly three lanes")
    if {row.get("lane") for row in rows} != LANES:
        raise ValueError("scoreboard must contain lane_1, lane_2, and lane_3 exactly once")
    if sorted(row.get("rank") for row in rows) != [1, 2, 3]:
        raise ValueError("scoreboard ranks must be exactly 1, 2, and 3")
    required = {
        "gross_theoretical_edge", "net_executable_edge", "sample", "outcomes",
        "execution", "stability", "capacity", "engineering_distance",
        "smallest_next_measurement", "sources", "promotion_verdict",
    }
    for row in rows:
        missing = required - set(row)
        if missing:
            raise ValueError(f"{row.get('lane')} missing fields: {sorted(missing)}")
        if not row["sources"]:
            raise ValueError(f"{row['lane']} must cite evidence")
def _v(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        value = f"{value:.6f}".rstrip("0").rstrip(".")
    return f"{value}{suffix}"


def render(data: dict[str, Any]) -> str:
    validate(data)
    lines = [
        "# Three-Lane Profitability Scoreboard",
        "",
        "Generated from `docs/ai-team/profitability_scoreboard.json`. Do not hand-edit.",
        "",
        f"**As of:** {data['as_of']}  ",
        f"**Policy:** `{data['policy_version']}`  ",
        "**Real trading:** DISABLED",
        "",
        f"> {data['ranking_basis']}",
        "",
        "| Rank | Lane | Gross theoretical edge | Net executable edge | Sample | Copyability / latency | Verdict | Distance |",
        "|---:|---|---|---|---|---|---|---|",
    ]
    for row in sorted(data["lanes"], key=lambda item: item["rank"]):
        gross, net, sample, execution = (row[key] for key in (
            "gross_theoretical_edge", "net_executable_edge", "sample", "execution"
        ))
        gross_text = f"{_v(gross['pnl_usd'], ' USD')}; avg {_v(gross['average_pnl_per_closed_trade_usd'], ' USD/close')}"
        net_text = f"{net['status']}: {_v(net['pnl_usd'], ' USD')}"
        sample_text = f"{_v(sample.get('closed_trades'))} closes; {_v(sample.get('unresolved_positions'))} unresolved"
        execution_text = f"{_v(execution['signal_to_shadow_open_rate_pct'], '%')}; median {_v(execution['observed_median_signal_latency_ms'], ' ms')}"
        lines.append(
            f"| {row['rank']} | {row['name']} | {gross_text} | {net_text} | "
            f"{sample_text} | {execution_text} | `{row['promotion_verdict']}` | "
            f"{row['engineering_distance']} |"
        )
    lines += [
        "",
        "## Interpretation",
        "",
        "Lane 3 ranks first for research effort because it alone has observed shadow closes and gross dollar PnL, not because executable profitability has been established. Lane 1 ranks second because frozen wallet-by-asset targets exist but their required economics are not in trusted evidence. Lane 2 ranks third because zero verified identities makes an execution comparison impossible.",
        "",
    ]
    for row in sorted(data["lanes"], key=lambda item: item["rank"]):
        gross, net, sample = row["gross_theoretical_edge"], row["net_executable_edge"], row["sample"]
        lines += [
            f"## {row['rank']}. {row['name']}",
            "",
            f"- Evidence: `{row['evidence_level']}`; verdict: `{row['promotion_verdict']}`.",
            f"- Gross theoretical: {_v(gross['pnl_usd'], ' USD')} PnL; {_v(gross['return_pct'], '%')} return; {_v(gross['average_pnl_per_closed_trade_usd'], ' USD')} average PnL per close. {gross['basis']}",
            f"- Net executable: {net['status']} — {net['reason']}",
            f"- Sample: {_v(sample.get('eligible_signals'))} eligible signals; {_v(sample.get('shadow_opens'))} opens; {_v(sample.get('closed_trades'))} closes; {_v(sample.get('unresolved_positions'))} unresolved; {_v(sample.get('distinct_days'))} distinct days.",
            f"- Outcomes/risk: win rate {_v(row['outcomes']['win_rate_pct'], '%')}; payoff ratio {_v(row['outcomes']['payoff_ratio'])}; max drawdown {_v(row['outcomes']['max_drawdown_usd'], ' USD')}; downside concentration {_v(row['outcomes']['downside_concentration'])}.",
            f"- Costs/latency/copyability: {row['execution']['fees']}; {row['execution']['slippage_spread_impact']}; funding {row['execution']['funding']}; {row['execution']['copyability_basis']}.",
            f"- Stability/confidence: {row['stability']['recency']}; degradation {row['stability']['degradation']}; {row['stability']['out_of_sample_status']}; {row['stability']['statistical_confidence']}.",
            f"- Capacity/concentration: {row['capacity']}.",
            f"- Smallest next measurement: {row['smallest_next_measurement']}",
            "- Sources: " + ", ".join(
                f"{source['source_type']} `{source['source_ref']}` observed {source['observed_at']}"
                for source in row["sources"]
            ) + ".",
            "",
        ]
    lines += ["## Candidate work order", ""]
    for candidate in data["priority_candidates"]:
        lines.append(f"{candidate['priority']}. **{candidate['candidate']}** — {candidate['why']} Status: `{candidate['status']}`.")
    lines += [
        "",
        "## Promotion and demotion",
        "",
        data["promotion_demotion"]["promotion"],
        "",
        data["promotion_demotion"]["demotion"],
        "",
        f"**Safety:** {data['promotion_demotion']['live_boundary']}",
        "",
        f"**Decision:** {data['decision']}",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    OUTPUT.write_text(render(data), encoding="utf-8")


if __name__ == "__main__":
    main()
