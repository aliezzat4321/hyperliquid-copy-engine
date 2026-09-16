#!/usr/bin/env python3
"""Bounded recovery for an already-installed GitHub Actions runner.

This helper is invoked by the independent host supervisor before the AI-team
orchestrator. It may restart an existing inactive/failed Actions runner systemd
unit. It must never register, remove, reconfigure, or rotate credentials for a
runner and never touches trading state.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

UTC = dt.timezone.utc  # noqa: UP017 -- production VM is Python 3.10
STATE_ROOT = Path(
    os.environ.get("AI_TEAM_SUPERVISOR_STATE_ROOT", "/var/lib/hyperliquid-ai-team-supervisor")
)
STATE_FILE = STATE_ROOT / "runner-recovery.json"
COOLDOWN_SECONDS = int(os.environ.get("AI_TEAM_RUNNER_RECOVERY_COOLDOWN_SECONDS", "600"))
VERIFY_SECONDS = int(os.environ.get("AI_TEAM_RUNNER_RECOVERY_VERIFY_SECONDS", "12"))
MAX_RUNNER_UNITS = int(os.environ.get("AI_TEAM_RUNNER_RECOVERY_MAX_UNITS", "8"))
RUNNER_UNIT_RE = re.compile(r"^actions\.runner\..+\.service$")


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr or "command timed out")


def load_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_state(state: dict[str, Any]) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def discover_runner_units() -> list[str]:
    cp = run(
        [
            "systemctl",
            "list-unit-files",
            "actions.runner.*.service",
            "--type=service",
            "--no-legend",
            "--no-pager",
        ]
    )
    if cp.returncode != 0:
        return []
    units: list[str] = []
    for line in cp.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        unit = fields[0]
        if RUNNER_UNIT_RE.fullmatch(unit) and unit not in units:
            units.append(unit)
    return units[:MAX_RUNNER_UNITS]


def active_state(unit: str) -> str:
    cp = run(["systemctl", "is-active", unit], timeout=10)
    return cp.stdout.strip() or "unknown"


def last_attempt_epoch(state: dict[str, Any], unit: str) -> int:
    attempts = state.get("attempts")
    if not isinstance(attempts, dict):
        return 0
    row = attempts.get(unit)
    if not isinstance(row, dict):
        return 0
    value = row.get("epoch")
    return int(value) if isinstance(value, int) else 0


def record_attempt(state: dict[str, Any], unit: str, now: dt.datetime) -> None:
    attempts = state.setdefault("attempts", {})
    if not isinstance(attempts, dict):
        attempts = {}
        state["attempts"] = attempts
    attempts[unit] = {"epoch": int(now.timestamp()), "at": iso(now)}


def recover_unit(unit: str, state: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    before = active_state(unit)
    row: dict[str, Any] = {"unit": unit, "before": before, "action": "none"}
    if before == "active":
        row["after"] = before
        return row

    now_epoch = int(now.timestamp())
    last_epoch = last_attempt_epoch(state, unit)
    if last_epoch and now_epoch - last_epoch < COOLDOWN_SECONDS:
        row.update(
            action="cooldown",
            after=before,
            cooldown_remaining_seconds=COOLDOWN_SECONDS - (now_epoch - last_epoch),
        )
        return row

    record_attempt(state, unit, now)
    run(["systemctl", "reset-failed", unit], timeout=10)
    restart = run(["systemctl", "restart", unit], timeout=30)
    row["action"] = "restart"
    row["restart_rc"] = restart.returncode

    deadline = time.monotonic() + max(1, VERIFY_SECONDS)
    after = active_state(unit)
    while after != "active" and time.monotonic() < deadline:
        time.sleep(1)
        after = active_state(unit)
    row["after"] = after
    row["recovered"] = restart.returncode == 0 and after == "active"
    return row


def main() -> int:
    now = utcnow()
    state = load_state()
    state["last_check_at"] = iso(now)
    state["real_trading_change"] = "NO"
    state["polymarket_touched"] = "NO"

    if os.geteuid() != 0:
        state["status"] = "DEGRADED_NOT_ROOT"
        save_state(state)
        print("RUNNER_RECOVERY=DEGRADED_NOT_ROOT")
        return 0

    units = discover_runner_units()
    state["discovered_units"] = units
    if not units:
        state["status"] = "NO_EXISTING_RUNNER_UNIT"
        state["last_results"] = []
        save_state(state)
        print("RUNNER_RECOVERY=NO_EXISTING_RUNNER_UNIT")
        return 0

    results = [recover_unit(unit, state, now) for unit in units]
    state["last_results"] = results
    failed = [row for row in results if row.get("action") == "restart" and not row.get("recovered")]
    recovered = [row for row in results if row.get("recovered")]
    if failed:
        state["status"] = "RECOVERY_FAILED"
    elif recovered:
        state["status"] = "RECOVERED"
        state["last_recovered_at"] = iso(now)
    else:
        state["status"] = "HEALTHY_OR_COOLDOWN"
    save_state(state)

    print(f"RUNNER_RECOVERY={state['status']}")
    print(f"RUNNER_UNITS={','.join(units)}")
    print("REAL_TRADING_CHANGE=NO")
    print("POLYMARKET_TOUCHED=NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
