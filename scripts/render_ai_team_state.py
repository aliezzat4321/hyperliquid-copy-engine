#!/usr/bin/env python3
"""Render the compact AI-team state and experiment index deterministically."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "docs/ai-team/state.json"
REGISTRY = ROOT / "docs/ai-team/experiments/registry.json"
STATE_OUT = ROOT / "docs/ai-team/CURRENT_STATE.md"
INDEX_OUT = ROOT / "docs/ai-team/experiments/INDEX.md"

LANE_TITLES = {
    "lane_1": "Lane 1 — Hyperliquid native discovery and prospective copying research",
    "lane_2": "Lane 2 — Third-party identity resolution",
    "lane_3": "Lane 3 — Direct Invo notification shadow copying",
}

SOURCE_LABEL = {
    "WORKFLOW_RUN": "run",
    "PULL_REQUEST": "PR",
    "ISSUE": "issue",
    "EXPERIMENT": "experiment",
    "COMMIT": "commit",
    "MANUAL_OBSERVATION": "manual",
}


def _value(fact: dict[str, Any]) -> str:
    value, unit = fact["value"], fact["unit"]
    if unit == "usd":
        return f"${value:,.6f}".rstrip("0").rstrip(".")
    if unit == "pct":
        return f"{value:.1f}%"
    if unit == "bps":
        return f"{value} bps"
    if unit == "count":
        return f"{value:,}"
    return str(value)


def _fact_rows(facts: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Fact | Value | Observed | Source |",
        "|---|---:|---|---|",
    ]
    for fact in facts:
        source = f"{SOURCE_LABEL[fact['source_type']]} `{fact['source_ref']}`"
        lines.append(
            f"| {fact['label']} | {_value(fact)} | `{fact['observed_at']}` | {source} |"
        )
    return lines


def _section(title: str, section: dict[str, Any]) -> list[str]:
    return [
        "",
        f"## {title}",
        f"**Status:** `{section['status']}`",
        "",
        *_fact_rows(section["facts"]),
        "",
        f"**Blocker:** {section['blocker']}  ",
        f"**Next:** {section['next']}.",
    ]


def render(data: dict[str, Any]) -> str:
    live = data["live_trading"]
    authorization = live["authorization"]
    lines = [
        "# Current State",
        "",
        "Generated from `docs/ai-team/state.json`. Do not hand-edit.",
        "",
        f"**Snapshot:** {data['snapshot_at']}  ",
        f"**Updated by:** {data['updated_by']}  ",
        f"**Observed main head:** `{data['head_observed']}`  ",
        f"**Mission:** {data['mission']}",
        "",
        "## Live trading",
        f"**{live['status']}** — user authorization: **{'YES' if live['authorized'] else 'NO'}**.",
    ]
    if authorization is not None:
        scope = authorization["scope"]
        lines += [
            "",
            f"Authorized by {authorization['authorized_by']} as "
            f"`{authorization['approval_reference']}` at {authorization['authorized_at']}, "
            f"expiring {authorization['expires_at']}.",
            "",
            f"Scope: {scope['lane']} / `{scope['slice']}` / {scope['service']} / "
            f"{scope['stage']}, maximum notional ${scope['max_notional_usd']:,}.",
        ]
    lines += [
        "",
        "## Active priorities",
        "| Priority | Issue | Objective | Builder | Reviewer | Status | Profit-critical |",
        "|---|---:|---|---|---|---|---|",
    ]
    for row in data["priorities"]:
        issue = f"#{row['issue']}" if row.get("issue") is not None else "—"
        critical = "yes" if row["profitability_critical"] else "no"
        lines.append(
            f"| {row['priority']} | {issue} | {row['title']} | {row['owner']} | "
            f"{row['reviewer']} | {row['status']} | {critical} |"
        )

    for key in ("lane_1", "lane_2", "lane_3"):
        lines += _section(LANE_TITLES[key], data["lanes"][key])
    lines += _section("Infrastructure", data["infrastructure"])

    lines += [
        "",
        "## Update rule",
        "Every fact above carries its own `observed_at` and source reference. The builder of "
        "any PR that materially changes these facts updates `state.json` in the same PR, with "
        "provenance. The independent reviewer verifies it. "
        "`scripts/render_ai_team_state.py` regenerates this file and CI rejects both drift and "
        "a snapshot older than the bound in `scripts/ai_team_contract.py`.",
        "",
    ]
    return "\n".join(lines)


def render_index(registry: dict[str, Any]) -> str:
    lines = [
        "# Experiment Index",
        "",
        "Generated from `docs/ai-team/experiments/registry.json`. Do not hand-edit.",
        "",
        "Check this index before proposing a hypothesis. Failed and inconclusive results are "
        "recorded here precisely so they are not silently repeated.",
        "",
        f"**Updated:** {registry['updated_at']}",
        "",
        "| ID | Lane | Status | Evidence | Result | Issue | PR | Builder | Reviewer "
        "| Reviewed commit |",
        "|---|---|---|---|---|---:|---:|---|---|---|",
    ]
    for row in sorted(registry["experiments"], key=lambda item: item["id"]):
        issue = f"#{row['issue']}" if row["issue"] is not None else "—"
        pr = f"#{row['pr']}" if row["pr"] is not None else "—"
        reviewer = row["reviewer"] or "—"
        commit = f"`{row['reviewed_commit'][:12]}`" if row["reviewed_commit"] else "—"
        lines.append(
            f"| {row['id']} | {row['lane']} | {row['status']} | {row['evidence_level']} | "
            f"{row['result']} | {issue} | {pr} | {row['builder']} | {reviewer} | {commit} |"
        )

    for row in sorted(registry["experiments"], key=lambda item: item["id"]):
        lines += [
            "",
            f"## {row['id']} — {row['lane']}",
            "",
            f"**Hypothesis:** {row['hypothesis']}",
            "",
            f"**Slice:** {row['slice']}",
            "",
            f"**Retest condition:** {row['retest_condition']}",
        ]
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    STATE_OUT.write_text(
        render(json.loads(STATE.read_text(encoding="utf-8"))), encoding="utf-8"
    )
    INDEX_OUT.write_text(
        render_index(json.loads(REGISTRY.read_text(encoding="utf-8"))), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
