#!/usr/bin/env python3
"""Project new durable AI-team ledger events to Trello without polling chat.

The existing SQLite/GitHub orchestration remains canonical. This relay only tails
append-only runtime event files and feeds material events to trello_team_bridge.py.
It is safe to retry and never exposes Trello credentials to model users.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

REPOSITORY = "aliezzat4321/hyperliquid-copy-engine"
EVENTS_DIR = Path("/var/lib/hyperliquid-ai-team/events")
CURSOR_PATH = Path("/var/lib/hyperliquid-ai-team/trello/event-cursor.json")
LEDGER_PATH = Path("/var/lib/hyperliquid-ai-team/orchestrator/ledger.sqlite3")
BRIDGE_PATH = Path("/opt/hyperliquid-ai-team/scripts/trello_team_bridge.py")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def load_cursor(path: Path) -> dict[str, int]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {str(k): max(0, int(v)) for k, v in raw.items()} if isinstance(raw, dict) else {}


def task_for_event(event: dict[str, Any], ledger: Path) -> dict[str, Any]:
    if not ledger.exists():
        return {}
    issue = event.get("issue")
    assignment = event.get("assignment_id")
    try:
        with sqlite3.connect(ledger) as db:
            db.row_factory = sqlite3.Row
            row = None
            if assignment:
                row = db.execute(
                    "SELECT * FROM tasks WHERE id=? LIMIT 1", (str(assignment),)
                ).fetchone()
            if row is None and issue is not None:
                row = db.execute(
                    "SELECT * FROM tasks WHERE issue_number=? ORDER BY updated_at DESC LIMIT 1",
                    (int(issue),),
                ).fetchone()
            return dict(row) if row is not None else {}
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return {}


def issue_metadata(issue: int) -> tuple[str, str]:
    title = "AI team task"
    priority = "P?"
    try:
        cp = subprocess.run(
            ["gh", "api", f"repos/{REPOSITORY}/issues/{issue}"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            row = json.loads(cp.stdout)
            title = str(row.get("title") or title)
            upper = title.upper()
            priority = "P0" if upper.startswith("P0") else "P1" if upper.startswith("P1") else "P?"
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    return title, priority


def normalize(event: dict[str, Any], ledger: Path) -> dict[str, Any] | None:
    raw_issue = event.get("issue")
    if raw_issue is None:
        return None
    try:
        issue = int(raw_issue)
    except (TypeError, ValueError):
        return None
    task = task_for_event(event, ledger)
    status = str(event.get("status") or task.get("status") or "PENDING").upper()
    task_type = str(task.get("task_type") or "").upper()
    kind = str(event.get("event") or "MATERIAL_EVENT").upper()

    normalized_kind = kind
    if kind == "RUN_STARTED":
        normalized_kind = (
            "REVIEW_STARTED"
            if task_type == "REVIEW"
            else "RESEARCH_STARTED"
            if task_type == "RESEARCH"
            else "BUILD_STARTED"
        )
    elif kind == "RUN_FINISHED" and task_type == "REVIEW":
        if status == "PASS":
            normalized_kind, status = "REVIEW_PASS", "WAITING_CI"
        elif status in {"FAIL", "FAILED"}:
            normalized_kind, status = "REVIEW_FAIL", "FAILED"
    elif kind in {"TASK_BLOCKED", "BLOCKED"}:
        normalized_kind, status = "BLOCKED", "BLOCKED"
    elif "RATE_LIMIT" in kind:
        normalized_kind, status = "RATE_LIMIT", "WAITING_RATE_LIMIT"
    elif "CI" in kind:
        normalized_kind = (
            "CI_PASS"
            if status in {"PASS", "SUCCESS", "DONE"}
            else "CI_FAIL"
            if status in {"FAIL", "FAILED", "ERROR"}
            else "CI_PENDING"
        )
    elif "MERG" in kind:
        normalized_kind, status = "MERGED", "DONE"

    title, priority = issue_metadata(issue)
    result = event.get("result") or event.get("error") or status or normalized_kind
    blocker = event.get("error") or task.get("last_error")
    model = task.get("model_class") or event.get("model")
    payload: dict[str, Any] = {
        "repository": REPOSITORY,
        "issue": issue,
        "event": normalized_kind,
        "priority": priority,
        "title": title,
        "task_type": task.get("task_type"),
        "agent": task.get("agent") or event.get("agent"),
        "model": model,
        "owner": task.get("agent") or event.get("agent") or "AI Team",
        "status": status,
        "pr": task.get("pr_number") or event.get("pr"),
        "sha": task.get("target_sha") or event.get("target_sha"),
        "result": str(result)[:800],
        "blocker": str(blocker)[:800] if blocker else "none",
        "next_action": "follow canonical GitHub/VM task state",
        "task_started_at": task.get("created_at"),
        "phase_started_at": event.get("at") or task.get("updated_at"),
    }
    if task_type == "REVIEW":
        payload["reviewer_model"] = f"CLAUDE / {model or 'SONNET'}"
    return payload


def bridge(payload: dict[str, Any], bridge_path: Path) -> bool:
    if not bridge_path.is_file():
        return False
    try:
        cp = subprocess.run(
            [sys.executable, str(bridge_path)],
            input=json.dumps(payload, separators=(",", ":")),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    output = (cp.stdout or "").strip()
    if output.startswith("TRELLO_SYNC=OK "):
        print(output)
        return True
    if output:
        print(output)
    return False


def initialize(events_dir: Path, cursor_path: Path) -> int:
    cursors: dict[str, int] = {}
    for path in sorted(events_dir.glob("*.jsonl")):
        try:
            cursors[path.name] = path.stat().st_size
        except OSError:
            continue
    atomic_json(cursor_path, cursors)
    print("TRELLO_EVENT_RELAY=INITIALIZED")
    return 0


def relay_once(
    events_dir: Path,
    cursor_path: Path,
    ledger: Path,
    bridge_path: Path,
) -> int:
    cursors = load_cursor(cursor_path)
    changed = False
    deferred = False
    for path in sorted(events_dir.glob("*.jsonl")):
        offset = cursors.get(path.name, 0)
        try:
            size = path.stat().st_size
            if offset > size:
                offset = 0
            with path.open("r", encoding="utf-8") as handle:
                handle.seek(offset)
                while True:
                    line_start = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    line_end = handle.tell()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        # Partial/invalid final lines are retried on the next filesystem event.
                        handle.seek(line_start)
                        deferred = True
                        break
                    payload = normalize(event, ledger)
                    if payload is not None and not bridge(payload, bridge_path):
                        handle.seek(line_start)
                        deferred = True
                        break
                    cursors[path.name] = line_end
                    changed = True
                if deferred:
                    break
        except OSError:
            deferred = True
            break
    if changed or not cursor_path.exists():
        atomic_json(cursor_path, cursors)
    print("TRELLO_EVENT_RELAY=DEFERRED" if deferred else "TRELLO_EVENT_RELAY=OK")
    return 75 if deferred else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--events-dir", type=Path, default=EVENTS_DIR)
    parser.add_argument("--cursor", type=Path, default=CURSOR_PATH)
    parser.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    parser.add_argument("--bridge", type=Path, default=BRIDGE_PATH)
    args = parser.parse_args()
    if args.initialize:
        return initialize(args.events_dir, args.cursor)
    return relay_once(args.events_dir, args.cursor, args.ledger, args.bridge)


if __name__ == "__main__":
    raise SystemExit(main())
