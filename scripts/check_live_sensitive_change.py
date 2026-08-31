#!/usr/bin/env python3
"""Detect changes that could affect real orders and require visible classification.

This guard **never authorizes trading**. It only refuses to let a change to real-
trading permissions, order routing, key handling, live service environment or safety
thresholds pass through a pull request that has not declared itself live-sensitive.

Capital authorization lives solely with the user, under
``docs/ai-team/LIVE_TRADING_GATE.md``.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Paths where any change is treated as live-sensitive by default.
LIVE_SENSITIVE_PATHS = (
    "src/hlcopy/trading/*",
    "services/*/src/hl-client.ts",
    "services/*/src/env.ts",
    "deploy/systemd/*",
    "scripts/deploy_*.sh",
    "scripts/bootstrap_*.sh",
    ".env.example",
)

#: Tokens whose appearance in an added or removed diff line is live-sensitive
#: wherever it occurs.
LIVE_SENSITIVE_TOKENS = (
    "REAL_TRADING_ENABLED",
    "NOTIFICATION_TRADER_LIVE",
    "HL_AGENT_KEY",
    "PRIVATE_KEY",
    "SECRET_KEY",
    "WALLET_ADDRESS",
    "placeOrder",
    "closePosition",
    "marketOrder",
    "exchange.order",
    "signL1Action",
    "maxSlippagePct",
    "maxNotionalUsd",
    "maxChaseBps",
    "maxPositions",
    "MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP",
)

#: Prose may name a live flag without changing one. Only code and config are scanned
#: for tokens; path rules still apply to every file.
DOC_SUFFIXES = frozenset({".md", ".rst", ".txt"})

#: Token scanning skips these because they *define* or *assert* the token list rather
#: than changing behaviour: this guard trips on itself otherwise. Neither can alter a
#: production code path, and the path rules above still apply to both.
TOKEN_SCAN_EXEMPT = (
    "scripts/check_live_sensitive_change.py",
    "tests/*",
)

CLASSIFICATION_RE = re.compile(r"^\s*LIVE-SENSITIVE:\s*(YES|NO)\s*$", re.IGNORECASE | re.MULTILINE)

MARKER = "LIVE-SENSITIVE: YES"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def changed_files(base: str) -> list[str]:
    try:
        raw = _git("diff", "--name-only", f"{base}...HEAD")
    except subprocess.CalledProcessError:
        raw = _git("diff", "--name-only", base, "HEAD")
    return [line for line in raw.splitlines() if line.strip()]


def changed_lines_by_file(base: str) -> dict[str, list[str]]:
    """Map each changed file to its added/removed diff lines."""
    try:
        raw = _git("diff", "-U0", f"{base}...HEAD")
    except subprocess.CalledProcessError:
        raw = _git("diff", "-U0", base, "HEAD")
    return parse_diff(raw)


def parse_diff(raw: str) -> dict[str, list[str]]:
    per_file: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            current = parts[1] if len(parts) == 2 else None
            if current:
                per_file.setdefault(current, [])
            continue
        if current is None or line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            per_file[current].append(line)
    return per_file


def scans_tokens(name: str) -> bool:
    """Documentation may discuss live flags; only code and config can change them."""
    if Path(name).suffix.lower() in DOC_SUFFIXES:
        return False
    return not any(fnmatch.fnmatch(name, pattern) for pattern in TOKEN_SCAN_EXEMPT)


def classify(files: list[str], lines_by_file: dict[str, list[str]]) -> list[str]:
    """Return human-readable reasons this change is live-sensitive."""
    reasons: list[str] = []
    for name in files:
        for pattern in LIVE_SENSITIVE_PATHS:
            if fnmatch.fnmatch(name, pattern):
                reasons.append(f"path {name} matches live-sensitive pattern {pattern}")
                break
    for name, lines in sorted(lines_by_file.items()):
        if not scans_tokens(name):
            continue
        for token in LIVE_SENSITIVE_TOKENS:
            if any(token in line for line in lines):
                reasons.append(f"{name} changes a line containing live-sensitive token {token!r}")
    return reasons


def declared(body: str) -> str | None:
    match = CLASSIFICATION_RE.search(body or "")
    return match.group(1).upper() if match else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check-live-sensitive-change")
    parser.add_argument("--base", default=os.environ.get("LIVE_GUARD_BASE", "origin/main"))
    args = parser.parse_args(argv)

    body = os.environ.get("PR_BODY", "")
    files = changed_files(args.base)
    reasons = classify(files, changed_lines_by_file(args.base))
    declaration = declared(body)

    print(f"LIVE_GUARD base={args.base} changed_files={len(files)}")
    for reason in reasons:
        print(f"LIVE_GUARD_REASON={reason}")
    print(f"LIVE_GUARD_DECLARED={declaration or 'ABSENT'}")

    if not reasons:
        print("LIVE_GUARD=NOT_LIVE_SENSITIVE")
        return 0

    if declaration != "YES":
        print(
            "LIVE_GUARD=FAIL This change touches real-trading permissions, order routing, "
            "key handling, live service environment or safety thresholds.\n"
            f"Add a line reading exactly '{MARKER}' to the pull request description, state "
            "the objective and scope, and link the user authorization required by "
            "docs/ai-team/LIVE_TRADING_GATE.md.\n"
            "This guard classifies only. It does not authorize trading."
        )
        return 1

    print(
        "LIVE_GUARD=CLASSIFIED_LIVE_SENSITIVE Declared in the pull request description. "
        "Classification is not authorization: capital still requires explicit user approval "
        "under docs/ai-team/LIVE_TRADING_GATE.md."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
