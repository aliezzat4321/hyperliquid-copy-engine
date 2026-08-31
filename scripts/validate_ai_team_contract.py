#!/usr/bin/env python3
"""Validate AI-team governance files and generated state."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "docs/ai-team/README.md",
    ROOT / "docs/ai-team/PROFITABILITY_STANDARD.md",
    ROOT / "docs/ai-team/LIVE_TRADING_GATE.md",
    ROOT / "docs/ai-team/DECISIONS.md",
    ROOT / "docs/ai-team/experiments/TEMPLATE.md",
    ROOT / "docs/ai-team/state.json",
    ROOT / "docs/ai-team/CURRENT_STATE.md",
]


def load_renderer():
    path = ROOT / "scripts/render_ai_team_state.py"
    spec = importlib.util.spec_from_file_location("render_ai_team_state", path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load state renderer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    missing = [str(p.relative_to(ROOT)) for p in REQUIRED if not p.exists()]
    if missing:
        raise SystemExit("missing AI-team contract files: " + ", ".join(missing))

    data = json.loads((ROOT / "docs/ai-team/state.json").read_text(encoding="utf-8"))
    for key in ("schema_version", "snapshot_at", "head_observed", "mission", "live_trading", "priorities", "lanes", "infrastructure"):
        if key not in data:
            raise SystemExit(f"state.json missing required key: {key}")

    live = data["live_trading"]
    if not isinstance(live.get("authorized"), bool):
        raise SystemExit("live_trading.authorized must be boolean")
    if live.get("authorized") and not live.get("approval_reference"):
        raise SystemExit("live authorization requires an explicit approval_reference")

    issues = [row.get("issue") for row in data["priorities"] if row.get("status") == "OPEN"]
    if any(issue is None for issue in issues):
        raise SystemExit("every OPEN priority must reference a GitHub Issue")
    if len(issues) != len(set(issues)):
        raise SystemExit("duplicate GitHub Issue in active priorities")

    renderer = load_renderer()
    expected = renderer.render(data)
    actual = (ROOT / "docs/ai-team/CURRENT_STATE.md").read_text(encoding="utf-8")
    if actual != expected:
        raise SystemExit("CURRENT_STATE.md drifted from state.json; run: python scripts/render_ai_team_state.py")

    print("AI_TEAM_CONTRACT_OK")


if __name__ == "__main__":
    main()
