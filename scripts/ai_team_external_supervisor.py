#!/usr/bin/env python3
"""Independent no-model supervisor for the Hyperliquid AI-team orchestrator.

This process intentionally lives outside the main orchestrator state machine. It only
observes GitHub/runtime health and systemd state, persists its own incident fingerprint,
and restarts/reconciles the orchestrator when a bounded stale condition is proven.
It never touches trading permissions, keys, capital, order routing, or Polymarket.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

REPO = "aliezzat4321/hyperliquid-copy-engine"
STATUS_ISSUE = 130
SERVICE = "hyperliquid-ai-team-orchestrator.service"
TIMER = "hyperliquid-ai-team-orchestrator.timer"
STATE_ROOT = Path(
    os.environ.get("AI_TEAM_SUPERVISOR_STATE_ROOT", "/var/lib/hyperliquid-ai-team-supervisor")
)
STATE_FILE = STATE_ROOT / "state.json"
LOCK_FILE = STATE_ROOT / "supervisor.lock"
ORCHESTRATOR = Path("/opt/hyperliquid-ai-team/scripts/ai_team_orchestrator.py")

STATUS_STALE_SECONDS = int(os.environ.get("AI_TEAM_SUPERVISOR_STATUS_STALE_SECONDS", "300"))
NO_PROGRESS_SECONDS = int(os.environ.get("AI_TEAM_SUPERVISOR_NO_PROGRESS_SECONDS", "600"))
SERVICE_HANG_SECONDS = int(os.environ.get("AI_TEAM_SUPERVISOR_SERVICE_HANG_SECONDS", "900"))
RECOVERY_COOLDOWN_SECONDS = int(
    os.environ.get("AI_TEAM_SUPERVISOR_RECOVERY_COOLDOWN_SECONDS", "180")
)
MAX_RECOVERY_ATTEMPTS = int(os.environ.get("AI_TEAM_SUPERVISOR_MAX_RECOVERY_ATTEMPTS", "3"))

ACTIVE_PROGRESS_STATUSES = {
    "PENDING",
    "RUNNING",
    "RETRY",
    "WAITING_CI",
    "WAITING_EVIDENCE_WINDOW",
    "WAITING_RATE_LIMIT",
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(dt.UTC)
    except ValueError:
        return None


def run(cmd: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode(errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr or "command timed out")


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any]) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def parse_runtime_body(body: str) -> dict[str, Any]:
    heartbeat_match = re.search(r"AI_TEAM_HEARTBEAT=([^>\s]+)", body)
    heartbeat = parse_time(heartbeat_match.group(1)) if heartbeat_match else None
    payload: dict[str, Any] = {}
    payload_match = re.search(r"```json\s*(\{.*\})\s*```", body, re.S)
    if payload_match:
        try:
            candidate = json.loads(payload_match.group(1))
            if isinstance(candidate, dict):
                payload = candidate
        except json.JSONDecodeError:
            pass
    return {"heartbeat": heartbeat, "payload": payload}


def runtime_material(payload: dict[str, Any]) -> dict[str, Any]:
    def agent(name: str) -> dict[str, Any]:
        row = payload.get(name)
        if not isinstance(row, dict):
            return {}
        return {
            key: row.get(key)
            for key in (
                "assignment_id",
                "issue",
                "pr",
                "status",
                "task_type",
                "target_sha",
                "retry_after",
                "updated_at",
                "last_progress",
                "blocker",
            )
        }

    recovery = payload.get("recovery")
    active_recovery = recovery.get("active_assignment") if isinstance(recovery, dict) else None
    if not isinstance(active_recovery, dict):
        active_recovery = {}
    return {
        "main_head": payload.get("main_head"),
        "pending_owner_action": payload.get("pending_owner_action"),
        "codex": agent("codex"),
        "claude": agent("claude"),
        "recovery": {
            key: active_recovery.get(key)
            for key in (
                "assignment_id",
                "issue",
                "status",
                "failure_class",
                "recovery_fingerprint",
                "updated_at",
            )
        },
    }


def fingerprint_material(payload: dict[str, Any]) -> str:
    raw = json.dumps(runtime_material(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def agent_is_actionable(row: Any, now: dt.datetime) -> bool:
    if not isinstance(row, dict):
        return False
    status = str(row.get("status") or "")
    if status not in ACTIVE_PROGRESS_STATUSES:
        return False
    if status == "RUNNING":
        return True
    retry_at = parse_time(row.get("retry_after"))
    if retry_at and retry_at > now:
        return False
    return True


def update_material_clock(
    state: dict[str, Any], payload: dict[str, Any], now: dt.datetime
) -> tuple[str, dt.datetime]:
    current = fingerprint_material(payload)
    previous = str(state.get("material_fingerprint") or "")
    since = parse_time(state.get("material_since"))
    if current != previous or since is None:
        since = now
        state["material_fingerprint"] = current
        state["material_since"] = iso(now)
    return current, since


def fetch_runtime_issue() -> tuple[str | None, str | None]:
    cp = run(["gh", "api", f"repos/{REPO}/issues/{STATUS_ISSUE}"], timeout=30)
    if cp.returncode != 0:
        return None, (cp.stderr or cp.stdout)[-1500:]
    try:
        row = json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        return None, f"invalid GitHub JSON: {exc}"
    body = row.get("body")
    if not isinstance(body, str):
        return None, "runtime issue body missing"
    return body, None


def systemd_value(unit: str, prop: str) -> str:
    cp = run(["systemctl", "show", unit, f"--property={prop}", "--value"], timeout=10)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def service_snapshot(now: dt.datetime) -> dict[str, Any]:
    timer_enabled = run(["systemctl", "is-enabled", TIMER], timeout=10).returncode == 0
    timer_active = run(["systemctl", "is-active", TIMER], timeout=10).returncode == 0
    service_active = run(["systemctl", "is-active", SERVICE], timeout=10).returncode == 0
    started_mono = systemd_value(SERVICE, "ActiveEnterTimestampMonotonic")
    service_age = 0.0
    if service_active and started_mono.isdigit():
        service_age = max(0.0, time.monotonic() - (int(started_mono) / 1_000_000.0))
    return {
        "observed_at": iso(now),
        "timer_enabled": timer_enabled,
        "timer_active": timer_active,
        "service_active": service_active,
        "service_age_seconds": round(service_age, 3),
    }


def prerequisites() -> list[str]:
    missing: list[str] = []
    if os.geteuid() != 0:
        missing.append("root")
    for binary in ("gh", "systemctl"):
        if not shutil.which(binary):
            missing.append(binary)
    if not ORCHESTRATOR.exists():
        missing.append(str(ORCHESTRATOR))
    if systemd_value(SERVICE, "LoadState") in {"", "not-found"}:
        missing.append(SERVICE)
    if systemd_value(TIMER, "LoadState") in {"", "not-found"}:
        missing.append(TIMER)
    return missing


def local_control_fault(systemd: dict[str, Any]) -> tuple[str | None, str | None]:
    if not systemd["timer_enabled"] or not systemd["timer_active"]:
        return "ORCHESTRATOR_TIMER_DOWN", "orchestrator timer is not enabled+active"
    if systemd["service_active"] and systemd["service_age_seconds"] > SERVICE_HANG_SECONDS:
        return (
            "ORCHESTRATOR_SERVICE_HUNG",
            f"orchestrator service active for {systemd['service_age_seconds']:.0f}s",
        )
    return None, None


def determine_fault(
    *,
    state: dict[str, Any],
    parsed: dict[str, Any],
    systemd: dict[str, Any],
    now: dt.datetime,
) -> tuple[str | None, str | None]:
    local_fault, local_detail = local_control_fault(systemd)
    if local_fault is not None:
        return local_fault, local_detail

    heartbeat = parsed.get("heartbeat")
    if isinstance(heartbeat, dt.datetime):
        age = max(0.0, (now - heartbeat).total_seconds())
        if age > STATUS_STALE_SECONDS:
            return "RUNTIME_STATUS_STALE", f"#130 heartbeat stale by {age:.0f}s"
    else:
        return "RUNTIME_STATUS_UNPARSEABLE", "#130 heartbeat missing/unparseable"

    payload = parsed.get("payload")
    if not isinstance(payload, dict) or not payload:
        return "RUNTIME_STATUS_UNPARSEABLE", "#130 machine payload missing/unparseable"

    _, material_since = update_material_clock(state, payload, now)
    no_progress_age = max(0.0, (now - material_since).total_seconds())
    actionable = any(agent_is_actionable(payload.get(name), now) for name in ("codex", "claude"))
    if actionable and no_progress_age > NO_PROGRESS_SECONDS:
        return (
            "ACTIONABLE_NO_PROGRESS",
            f"actionable runtime material unchanged for {no_progress_age:.0f}s",
        )
    return None, None


def incident_fingerprint(kind: str, parsed: dict[str, Any], systemd: dict[str, Any]) -> str:
    del systemd  # transient systemd/heartbeat changes must not reset the recovery budget
    payload = parsed.get("payload") if isinstance(parsed.get("payload"), dict) else {}
    raw = {
        "kind": kind,
        "main_head": payload.get("main_head"),
        "material": fingerprint_material(payload) if payload else "",
    }
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()


def recovery_allowed(state: dict[str, Any], fp: str, now: dt.datetime) -> tuple[bool, str]:
    incident = state.get("incident")
    if not isinstance(incident, dict) or incident.get("fingerprint") != fp:
        return True, "new incident"
    attempts = int(incident.get("attempts") or 0)
    if attempts >= MAX_RECOVERY_ATTEMPTS:
        return False, "recovery budget exhausted"
    last = parse_time(incident.get("last_recovery_at"))
    if last and (now - last).total_seconds() < RECOVERY_COOLDOWN_SECONDS:
        return False, "recovery cooldown active"
    return True, "retry incident"


def persist_incident(
    state: dict[str, Any], *, kind: str, detail: str, fp: str, now: dt.datetime
) -> dict[str, Any]:
    incident = state.get("incident")
    if not isinstance(incident, dict) or incident.get("fingerprint") != fp:
        incident = {
            "fingerprint": fp,
            "kind": kind,
            "detail": detail,
            "first_seen_at": iso(now),
            "attempts": 0,
            "status": "OPEN",
        }
    incident["last_seen_at"] = iso(now)
    incident["detail"] = detail
    state["incident"] = incident
    return incident


def recover(systemd: dict[str, Any], *, kind: str) -> tuple[str, str]:
    actions: list[str] = []
    if not systemd["timer_enabled"] or not systemd["timer_active"]:
        cp = run(["systemctl", "enable", "--now", TIMER], timeout=30)
        if cp.returncode != 0:
            return "FAILED", f"enable timer failed: {(cp.stderr or cp.stdout)[-800:]}"
        actions.append("timer-enabled")

    if systemd["service_active"]:
        if kind != "ORCHESTRATOR_SERVICE_HUNG":
            return "DEFERRED", "service still active; defer recovery until hang threshold"
        cp = run(["systemctl", "stop", SERVICE], timeout=30)
        if cp.returncode != 0:
            run(["systemctl", "kill", "--kill-who=main", SERVICE], timeout=10)
            cp = run(["systemctl", "stop", SERVICE], timeout=30)
            if cp.returncode != 0:
                return "FAILED", (
                    f"unable to stop hung orchestrator: {(cp.stderr or cp.stdout)[-800:]}"
                )
        actions.append("hung-service-stopped")

    run(["systemctl", "reset-failed", SERVICE], timeout=10)
    cp = run(["systemctl", "start", "--no-block", SERVICE], timeout=30)
    if cp.returncode != 0:
        return "FAILED", f"orchestrator start failed: {(cp.stderr or cp.stdout)[-1200:]}"
    actions.append("orchestrator-cycle-queued")
    return "TRIGGERED", ",".join(actions)


def record_recovery_outcome(
    incident: dict[str, Any],
    state: dict[str, Any],
    *,
    outcome_state: str,
    outcome: str,
    now: dt.datetime,
) -> None:
    if outcome_state != "DEFERRED":
        incident["attempts"] = int(incident.get("attempts") or 0) + 1
        incident["last_recovery_at"] = iso(now)
        incident["last_recovery_outcome"] = outcome
    state["last_error"] = outcome if outcome_state == "FAILED" else None


def healthy_state(state: dict[str, Any], now: dt.datetime, systemd: dict[str, Any]) -> None:
    previous = state.get("incident")
    if isinstance(previous, dict) and previous.get("status") == "OPEN":
        previous["status"] = "RECOVERED"
        previous["recovered_at"] = iso(now)
        state["last_recovered_incident"] = previous
    state["incident"] = None
    state["last_healthy_at"] = iso(now)
    state["last_systemd"] = systemd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)

    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_ROOT, 0o700)
    with LOCK_FILE.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("SUPERVISOR_STATE=ALREADY_RUNNING")
            return 0

        now = utcnow()
        state = load_state()
        state["last_check_at"] = iso(now)
        missing = prerequisites()
        if missing:
            state["last_error"] = "missing prerequisites: " + ",".join(missing)
            save_state(state)
            print("SUPERVISOR_STATE=DEGRADED")
            print("MISSING_PREREQUISITES=" + ",".join(missing))
            return 2

        systemd = service_snapshot(now)
        state["last_systemd"] = systemd
        local_fault, local_detail = local_control_fault(systemd)
        if local_fault is not None:
            parsed = {"heartbeat": None, "payload": {}}
            fault, detail = local_fault, local_detail
        else:
            body, error = fetch_runtime_issue()
            if error or body is None:
                state["last_error"] = f"runtime status fetch failed: {error}"
                save_state(state)
                print("SUPERVISOR_STATE=DEPENDENCY_WAIT")
                print("GITHUB_STATUS_FETCH=FAILED")
                return 0
            parsed = parse_runtime_body(body)
            fault, detail = determine_fault(state=state, parsed=parsed, systemd=systemd, now=now)

        heartbeat = parsed.get("heartbeat")
        state["last_runtime_heartbeat"] = (
            iso(heartbeat) if isinstance(heartbeat, dt.datetime) else None
        )
        if fault is None:
            healthy_state(state, now, systemd)
            state["last_error"] = None
            save_state(state)
            print("SUPERVISOR_STATE=HEALTHY")
            print("ORCHESTRATOR_TIMER=ACTIVE")
            print("REAL_TRADING_CHANGE=NO")
            print("POLYMARKET_TOUCHED=NO")
            return 0

        fp = incident_fingerprint(fault, parsed, systemd)
        incident = persist_incident(state, kind=fault, detail=detail or fault, fp=fp, now=now)
        allowed, why = recovery_allowed(state, fp, now)
        if args.check_only:
            save_state(state)
            print("SUPERVISOR_STATE=FAULT")
            print(f"FAULT={fault}")
            print("CHECK_ONLY=YES")
            return 1

        if not allowed:
            if why == "recovery budget exhausted":
                incident["status"] = "ESCALATED"
            state["last_error"] = why
            save_state(state)
            print(f"SUPERVISOR_STATE={incident.get('status', 'OPEN')}")
            print(f"FAULT={fault}")
            print(f"RECOVERY={why}")
            return 3 if incident.get("status") == "ESCALATED" else 0

        outcome_state, outcome = recover(systemd, kind=fault)
        record_recovery_outcome(
            incident, state, outcome_state=outcome_state, outcome=outcome, now=now
        )
        save_state(state)
        print(f"SUPERVISOR_STATE=RECOVERY_{outcome_state}")
        print(f"FAULT={fault}")
        print(f"RECOVERY_OUTCOME={outcome}")
        print("REAL_TRADING_CHANGE=NO")
        print("POLYMARKET_TOUCHED=NO")
        return 0 if outcome_state in {"TRIGGERED", "DEFERRED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
