#!/usr/bin/env python3
"""Render the compact AI-team state deterministically from state.json."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "docs/ai-team/state.json"
OUT = ROOT / "docs/ai-team/CURRENT_STATE.md"


def render(data: dict) -> str:
    lines = [
        "# Current State",
        "",
        "Generated from `docs/ai-team/state.json`. Do not hand-edit.",
        "",
        f"**Snapshot:** {data['snapshot_at']}  ",
        f"**Observed main head:** `{data['head_observed']}`  ",
        f"**Mission:** {data['mission']}",
        "",
        "## Live trading",
        f"**{data['live_trading']['status']}** — user authorization: **{'YES' if data['live_trading']['authorized'] else 'NO'}**.",
        "",
        "## Active priorities",
        "| Priority | Issue | Objective | Builder | Reviewer | Status |",
        "|---|---:|---|---|---|---|",
    ]
    for row in data["priorities"]:
        issue = f"#{row['issue']}" if row.get("issue") is not None else "—"
        lines.append(f"| {row['priority']} | {issue} | {row['title']} | {row['owner']} | {row['reviewer']} | {row['status']} |")

    lane_titles = {
        "lane_1": "Lane 1 — Hyperliquid native discovery and prospective copying research",
        "lane_2": "Lane 2 — Third-party identity resolution",
        "lane_3": "Lane 3 — Direct Invo notification shadow copying",
    }
    for key in ("lane_1", "lane_2", "lane_3"):
        lane = data["lanes"][key]
        lines += [
            "",
            f"## {lane_titles[key]}",
            f"**Status:** `{lane['status']}`",
            "",
            "Facts: " + "; ".join(lane["facts"]) + ".",
            "",
            f"**Blocker:** {lane['blocker']}  ",
            f"**Next:** {lane['next']}.",
        ]

    infra = data["infrastructure"]
    lines += [
        "",
        "## Infrastructure",
        f"`{infra['data_volume']}` was observed at **{infra['data_volume_usage_pct_observed']:.1f}%** usage at `{infra['data_volume_observed_at']}`.",
        "",
        f"**Status:** `{infra['status']}`  ",
        f"**Next:** {infra['next']}.",
        "",
        "## Update rule",
        "The builder of any PR that materially changes these facts updates `state.json` in the same PR. The independent reviewer verifies it. `scripts/render_ai_team_state.py` regenerates this file and CI rejects drift.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    data = json.loads(STATE.read_text(encoding="utf-8"))
    OUT.write_text(render(data), encoding="utf-8")


if __name__ == "__main__":
    main()
