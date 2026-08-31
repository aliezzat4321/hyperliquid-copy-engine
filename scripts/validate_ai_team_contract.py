#!/usr/bin/env python3
"""CI entry point for the shared AI-team contract.

Rules live in ``scripts/ai_team_contract.py`` so they are importable by tests.
This file wires them to the repository: required files, generated-file drift, and
verification that ``head_observed`` is a commit this checkout can actually see.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ai_team_contract import ContractError, validate_experiments, validate_state  # noqa: E402
from render_ai_team_state import render, render_index  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "docs/ai-team/state.json"
REGISTRY_PATH = ROOT / "docs/ai-team/experiments/registry.json"

REQUIRED = [
    "AGENTS.md",
    "CLAUDE.md",
    "docs/ai-team/README.md",
    "docs/ai-team/SYSTEM_MAP.md",
    "docs/ai-team/PROFITABILITY_STANDARD.md",
    "docs/ai-team/PROMOTION_POLICY.md",
    "docs/ai-team/promotion_policy.json",
    "docs/ai-team/LIVE_TRADING_GATE.md",
    "docs/ai-team/REVIEW_PROVENANCE.md",
    "docs/ai-team/DECISIONS.md",
    "docs/ai-team/experiments/TEMPLATE.md",
    "docs/ai-team/experiments/registry.json",
    "docs/ai-team/experiments/INDEX.md",
    "docs/ai-team/state.json",
    "docs/ai-team/CURRENT_STATE.md",
]


def _head_is_known(sha: str) -> bool:
    """True when this checkout can resolve the SHA.

    CI fetches full history so an unknown SHA is a real error. When git is absent
    or the repository is shallow, the check is skipped rather than guessed at.
    """
    try:
        depth = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return True
    if depth == "true":
        print("AI_TEAM_CONTRACT_NOTE=shallow checkout; skipping head_observed verification")
        return True
    return subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=ROOT, capture_output=True,
    ).returncode == 0


def _check_generated(path: Path, expected: str) -> None:
    actual = path.read_text(encoding="utf-8")
    if actual != expected:
        raise ContractError(
            f"{path.relative_to(ROOT)} drifted from its source; "
            "run: python scripts/render_ai_team_state.py"
        )


def run() -> None:
    missing = [name for name in REQUIRED if not (ROOT / name).exists()]
    if missing:
        raise ContractError(f"missing AI-team contract files: {missing}")

    for name in REQUIRED:
        path = ROOT / name
        if path.suffix == ".md" and not path.read_bytes().endswith(b"\n"):
            raise ContractError(f"{name} must end with a newline")

    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    validate_state(state)
    validate_experiments(registry)

    if not _head_is_known(state["head_observed"]):
        raise ContractError(
            f"state.json head_observed {state['head_observed']} is not a commit in this "
            "repository; record a real observed head"
        )

    _check_generated(ROOT / "docs/ai-team/CURRENT_STATE.md", render(state))
    _check_generated(ROOT / "docs/ai-team/experiments/INDEX.md", render_index(registry))

    policy = json.loads((ROOT / "docs/ai-team/promotion_policy.json").read_text(encoding="utf-8"))
    for key in ("policy_id", "policy_version", "status", "thresholds", "review_trigger"):
        if key not in policy:
            raise ContractError(f"promotion_policy.json missing required key: {key}")

    print("AI_TEAM_CONTRACT_OK")


def main() -> None:
    try:
        run()
    except ContractError as exc:
        raise SystemExit(f"AI_TEAM_CONTRACT_FAIL: {exc}") from exc


if __name__ == "__main__":
    main()
