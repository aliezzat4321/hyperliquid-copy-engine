#!/usr/bin/env python3
"""Event-driven, fail-open Trello projection for the AI-team VM ledger.

This is an observability adapter, not an orchestrator.  It consumes one normalized
material event at a time and projects it onto the card mapped to a GitHub issue.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPOSITORY = "aliezzat4321/hyperliquid-copy-engine"
BOARD_ID = "6a9713c265a75ed50d4181d7"
LISTS = {
    "BACKLOG": "6a9713db6cfc74eee5b812b1",
    "IN_PROGRESS": "6a9713e3666e881387b18b9a",
    "REVIEW_CI": "6a9713fdfc33292cc90f5486",
    "BLOCKED": "6a9713f546dc3d1c3907c634",
    "DONE": "6a97140a2df53d4869073c91",
}
OWNER = "aliezzat2"
DEFAULT_STATE = Path("/var/lib/hyperliquid-ai-team/trello/bridge.json")
DEFAULT_FAILURES = Path("/var/lib/hyperliquid-ai-team/trello/sync-failures.jsonl")
DEFAULT_LEDGER = Path("/var/lib/hyperliquid-ai-team/orchestrator/ledger.sqlite3")
DEFAULT_OUTBOX = Path("/var/lib/hyperliquid-ai-team/trello-outbox")
NOTIFY = {
    "BLOCKED",
    "OWNER_ACTION",
    "REVIEW_FAIL",
    "REVIEW_PASS",
    "CI_FAIL",
    "MERGED",
    "COMPLETED",
    "SIGNIFICANT_RESULT",
}
FALLBACKS = {
    ("CODEX", "BUILD"): (5, 15),
    ("CODEX", "REPAIR"): (5, 12),
    ("CLAUDE", "REVIEW_SONNET"): (2, 7),
    ("CLAUDE", "REVIEW_OPUS"): (4, 10),
    ("SYSTEM", "CI_MERGE"): (3, 10),
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)  # noqa: UP017 - VM supports Python 3.10


def iso(value: dt.datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (
            parsed
            if parsed.tzinfo
            else parsed.replace(tzinfo=dt.timezone.utc)  # noqa: UP017 - Python 3.10
        )
    except ValueError:
        return None


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"cards": {}}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "cards": {}}


def phase(event: dict[str, Any]) -> str:
    kind = str(event.get("event", "")).upper()
    status = str(event.get("status", "")).upper()
    if kind in {"COMPLETED", "MERGED"} or status == "DONE":
        return "DONE"
    if kind in {"BLOCKED", "OWNER_ACTION", "AUTH_FAILURE"} or status in {"BLOCKED", "FAILED"}:
        return "BLOCKED"
    review_events = {
        "PR_OPENED", "REVIEW_STARTED", "REVIEW_PASS", "REVIEW_FAIL",
        "CI_PENDING", "CI_PASS", "CI_FAIL",
    }
    if kind in review_events or event.get("pr"):
        return "REVIEW_CI"
    active_events = {
        "ASSIGNED", "TASK_ASSIGNED", "BUILD_STARTED", "RESEARCH_STARTED",
        "RUN_STARTED", "RETRY", "RATE_LIMIT",
    }
    if kind in active_events or status in {
        "PENDING", "RUNNING", "RETRY", "WAITING_RATE_LIMIT"
    }:
        return "IN_PROGRESS"
    return "BACKLOG"


def eta_key(event: dict[str, Any]) -> tuple[str, str] | None:
    kind = str(event.get("event", "")).upper()
    task_type = str(event.get("task_type", "")).upper()
    agent = str(event.get("agent", "")).upper()
    model = str(event.get("model", event.get("model_class", ""))).upper()
    if kind.startswith("CI") or kind == "MERGED" or task_type == "CI_MERGE":
        return ("SYSTEM", "CI_MERGE")
    if task_type == "REVIEW" or kind.startswith("REVIEW"):
        return ("CLAUDE", "REVIEW_OPUS" if "OPUS" in model else "REVIEW_SONNET")
    if task_type in {"BUILD", "REPAIR"}:
        return ("CODEX", task_type)
    if agent.startswith("CODEX"):
        return ("CODEX", "BUILD")
    return None


def historical_band(ledger: Path, event: dict[str, Any]) -> tuple[int, int, int] | None:
    key = eta_key(event)
    if not key or not ledger.exists() or key == ("SYSTEM", "CI_MERGE"):
        return None
    task_type = "REVIEW" if key[1].startswith("REVIEW") else key[1]
    agent_prefix = "CLAUDE%" if key[0] == "CLAUDE" else "CODEX%"
    try:
        with sqlite3.connect(ledger) as db:
            rows = db.execute(
                """SELECT (julianday(r.ended_at)-julianday(r.started_at))*1440
                   FROM runs r JOIN tasks t ON t.id=r.task_id
                   WHERE r.exit_code=0 AND r.ended_at IS NOT NULL
                     AND t.task_type=? AND upper(t.agent) LIKE ?
                   ORDER BY r.id DESC LIMIT 30""",
                (task_type, agent_prefix),
            ).fetchall()
    except (sqlite3.Error, OSError):
        return None
    samples = sorted(max(1, round(float(row[0]))) for row in rows if row[0] is not None)
    if len(samples) < 5:
        return None
    low = samples[max(0, len(samples) // 10 - 1)]
    high = samples[min(len(samples) - 1, (len(samples) * 9) // 10)]
    return low, high, len(samples)


def eta(event: dict[str, Any], ledger: Path, now: dt.datetime) -> tuple[str, str, bool]:
    if event.get("eta_minutes") is not None:
        low = high = max(0, int(event["eta_minutes"]))
        source = "explicit measured estimate"
    else:
        key = eta_key(event)
        historical = historical_band(ledger, event)
        if historical:
            low, high, count = historical
            source = f"runtime ledger n={count} (p10-p90)"
        elif key in FALLBACKS:
            low, high = FALLBACKS[key]
            source = "conservative fallback"
        else:
            return "measured estimate required", "not estimated", False
    started = parse_time(event.get("phase_started_at") or event.get("started_at")) or now
    elapsed = max(0, int((now - started).total_seconds() // 60))
    over = elapsed > high and phase(event) not in {"DONE", "BLOCKED"}
    checkpoint = iso(started + dt.timedelta(minutes=high))
    return f"{low}–{high} min ({source})", checkpoint, over


def description(event: dict[str, Any], now: dt.datetime, ledger: Path) -> str:
    started = parse_time(event.get("task_started_at") or event.get("created_at")) or now
    elapsed = max(0, int((now - started).total_seconds() // 60))
    band, checkpoint, over = eta(event, ledger, now)
    status = str(event.get("status") or phase(event)) + (" / OVER_ETA" if over else "")
    issue = int(event["issue"])
    pr = f"#{event['pr']}" if event.get("pr") else "none"
    sha = str(event.get("sha") or event.get("target_sha") or "none")[:40]
    fields = [
        ("Priority", event.get("priority", "unspecified")),
        ("Issue", f"#{issue} https://github.com/{REPOSITORY}/issues/{issue}"),
        ("PR / SHA", f"{pr} / {sha}"),
        ("Owner", event.get("owner", "unassigned")),
        ("Reviewer / model", event.get("reviewer_model") or event.get("model") or "unassigned"),
        ("Status", status),
        ("Latest result", event.get("result", "none")),
        ("Blocker", event.get("blocker", "none")),
        ("Next action", event.get("next_action", "await next material event")),
        ("Elapsed time", f"{elapsed} min"),
        ("ETA band", band),
        ("Expected next checkpoint", event.get("next_checkpoint", checkpoint)),
        ("Last updated", iso(now)),
    ]
    return "\n".join(f"{name}: {value}" for name, value in fields)


class Trello:
    def __init__(
        self,
        key: str,
        token: str,
        request: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.key, self.token, self.request = key, token, request

    def call(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        params = {"key": self.key, "token": self.token, **(data or {})}
        body = urllib.parse.urlencode(params).encode() if method != "GET" else None
        url = "https://api.trello.com/1" + path
        if method == "GET":
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=body, method=method)
        for attempt in range(3):
            try:
                with self.request(req, timeout=15) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    raise
                if attempt == 2:
                    raise
                retry = min(4, max(1, int(exc.headers.get("Retry-After", "1"))))
                time.sleep(retry)
            except urllib.error.URLError:
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError("unreachable")

    def exact_issue_card(self, issue: int) -> str | None:
        """Reuse the board's existing exact-issue card when local state was lost."""
        rows = self.call(
            "GET", f"/boards/{BOARD_ID}/cards", {"fields": "id,name,desc", "filter": "open"}
        ) or []
        title_marker = re.compile(rf"(?:^|\s)#{issue}(?:\s|$)")
        issue_field = re.compile(rf"(?m)^Issue:\s*#{issue}(?:\s|$)")
        matches = sorted(
            str(row["id"])
            for row in rows
            if title_marker.search(str(row.get("name", "")))
            or issue_field.search(str(row.get("desc", "")))
        )
        return matches[0] if matches else None


def sync(event: dict[str, Any], client: Trello, state_path: Path, ledger: Path) -> dict[str, Any]:
    if event.get("repository", REPOSITORY) != REPOSITORY:
        raise ValueError("repository mismatch")
    issue = int(event["issue"])
    now = utcnow()
    state = load_state(state_path)
    cards = state.setdefault("cards", {})
    key = f"{REPOSITORY}#{issue}"
    card_id = cards.get(key)
    title = f"[{event.get('priority', 'P?')}] #{issue} {event.get('title', 'AI team task')}"
    payload = {
        "name": title,
        "desc": description(event, now, ledger),
        "idList": LISTS[phase(event)],
    }
    if not card_id:
        card_id = client.exact_issue_card(issue)
        if card_id:
            cards[key] = card_id
    if card_id:
        client.call("PUT", f"/cards/{card_id}", payload)
    else:
        card = client.call("POST", "/cards", payload)
        card_id = str(card["id"])
        cards[key] = card_id
    state.update({"version": 1, "last_success_at": iso(now), "last_error": None})
    atomic_json(state_path, state)
    kind = str(event.get("event", "")).upper()
    if kind in NOTIFY:
        summary = str(
            event.get("result")
            or event.get("blocker")
            or event.get("next_action")
            or kind
        )
        client.call(
            "POST",
            f"/cards/{card_id}/actions/comments",
            {"text": f"@{OWNER} {kind}: {summary[:500]}"},
        )
    return {"card_id": card_id, "issue": issue, "list": phase(event), "notified": kind in NOTIFY}


def reconcile(
    outbox: Path, client: Trello, state_path: Path, ledger: Path, limit: int = 50
) -> dict[str, int]:
    """Drain a bounded durable outbox; retain failures for a later convergence pass."""
    processed = deferred = 0
    failed_issues: set[int] = set()
    for path in sorted(outbox.glob("*.json"))[: max(0, limit)]:
        event: Any = None
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
            issue = int(event["issue"])
            if issue in failed_issues:
                deferred += 1
                continue
            sync(event, client, state_path, ledger)
            path.unlink()
            processed += 1
        except Exception:
            deferred += 1
            if isinstance(event, dict) and event.get("issue") is not None:
                try:
                    failed_issues.add(int(event["issue"]))
                except (TypeError, ValueError):
                    pass
            continue
    return {"processed": processed, "deferred": deferred}


def record_failure(path: Path, event: dict[str, Any], error: Exception) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "at": iso(utcnow()),
        "issue": event.get("issue"),
        "event": event.get("event"),
        "error": type(error).__name__,
        "retry": "bounded backoff on next material event",
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-file", type=Path, help="JSON event; stdin when omitted")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--failures", type=Path, default=DEFAULT_FAILURES)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--reconcile-dir", type=Path)
    parser.add_argument("--max-events", type=int, default=50)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/etc/hyperliquid-ai-team/trello.env"),
    )
    return parser.parse_args()


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip()
    return values


def main() -> int:
    args = parse_args()
    event: dict[str, Any] = {}
    try:
        credentials = read_env(args.env_file)
        client = Trello(credentials["TRELLO_API_KEY"], credentials["TRELLO_TOKEN"])
        if args.reconcile_dir:
            args.state.parent.mkdir(parents=True, exist_ok=True)
            with (args.state.parent / "reconcile.lock").open("a+") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    result = {"processed": 0, "deferred": 0}
                else:
                    result = reconcile(
                        args.reconcile_dir, client, args.state, args.ledger,
                        args.max_events,
                    )
        else:
            event = json.loads(
                args.event_file.read_text() if args.event_file else sys.stdin.read()
            )
            result = sync(event, client, args.state, args.ledger)
    except Exception as exc:  # Trello observability must never block canonical work.
        record_failure(args.failures, event, exc)
        print(f"TRELLO_SYNC=DEFERRED issue={event.get('issue')} reason={type(exc).__name__}")
        return 0
    print("TRELLO_SYNC=OK " + json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
