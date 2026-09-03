#!/usr/bin/env python3
"""Durable file/checkpoint projection for the existing Hyperliquid AI-team ledger.

This module is deliberately not an orchestrator. The SQLite ledger in
ai_team_orchestrator.py remains the execution source of truth. This module
projects that state into bounded, redacted files and a compact handoff payload
so a fresh model/chat can recover without conversation history.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

MAX_RAW_BYTES = 256 * 1024
MAX_HANDOFF_BYTES = 3900
ACTIVE = {"PENDING", "RETRY", "WAITING_RATE_LIMIT", "WAITING_CI", "RUNNING"}

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "[REDACTED_ANTHROPIC]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]+"), "[REDACTED_GITHUB]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]+"), "[REDACTED_GITHUB]"),
    (re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (
        re.compile(
            r"(?i)\b(authorization|cookie|api[_-]?key|oauth[_-]?token|access[_-]?token|"
            r"refresh[_-]?token|client[_-]?secret)\b\s*[:=]\s*([^\s,;]+)"
        ),
        r"\1=[REDACTED]",
    ),
)


def utcnow() -> str:
    now = dt.datetime.now(dt.timezone.utc)  # noqa: UP017 - VM supports Python 3.10
    return now.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact(text: str | None) -> str:
    value = text or ""
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def bounded_redacted(text: str | None, limit: int = MAX_RAW_BYTES) -> str:
    value = redact(text)
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return value
    suffix = b"\n[TRUNCATED_BY_AI_TEAM_RUNTIME_LEDGER]\n"
    keep = max(0, limit - len(suffix))
    return (raw[-keep:] + suffix).decode("utf-8", errors="replace")


def _mkdir(path: Path, mode: int | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if mode is None:
        return
    try:
        path.chmod(mode)
    except PermissionError:
        pass


def _atomic_text(path: Path, text: str, mode: int = 0o600) -> None:
    _mkdir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(mode)
    os.replace(tmp, path)


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _row_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def _safe_blockers(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [redact(str(x))[:1000] for x in value]
    try:
        raw = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return []
    return [redact(str(x))[:1000] for x in raw] if isinstance(raw, list) else []


def _agent_key(agent: str) -> str:
    return "claude" if agent.upper().startswith("CLAUDE") else "codex"


def _slot(task: dict[str, Any]) -> str:
    agent = str(task.get("agent") or "")
    if agent == "CODEX_REVIEWER":
        return "codex_reviewer"
    if agent.startswith("CODEX"):
        return "codex_builder"
    return "opus" if str(task.get("model_class")) == "OPUS" else "sonnet"


def _role(task: dict[str, Any]) -> str:
    if str(task.get("task_type")) in {"CHALLENGE", "FINAL_REVIEW"}:
        return "QUANT" if str(task.get("model_class")) == "OPUS" else "REVIEWER"
    if str(task.get("task_type")) == "REVIEW":
        if str(task.get("task_class")) in {
            "QUANT_PROFITABILITY",
            "STATISTICAL_METHODOLOGY",
            "CAPITAL_SENSITIVE_METHODOLOGY",
        }:
            return "QUANT"
        return "REVIEWER"
    if str(task.get("task_type")) in {"BUILD", "REPAIR"}:
        return "BUILDER"
    return "OPS"


def _current_step(task: dict[str, Any]) -> str:
    status = str(task.get("status") or "UNKNOWN")
    task_type = str(task.get("task_type") or "TASK").lower()
    return {
        "PENDING": f"awaiting {task_type} dispatch",
        "RETRY": f"retrying {task_type}",
        "RUNNING": f"{task_type} model process running",
        "WAITING_RATE_LIMIT": f"{task_type} paused for model rate/usage limit",
        "WAITING_CI": "exact-SHA review passed; waiting for CI/merge gate",
        "BLOCKED": f"{task_type} blocked",
        "DONE": f"{task_type} complete",
        "STALE": f"{task_type} stale; replacement assignment required",
    }.get(status, f"{task_type} status {status.lower()}")


def _next_step(task: dict[str, Any]) -> str:
    status = str(task.get("status") or "UNKNOWN")
    task_type = str(task.get("task_type") or "")
    if status == "WAITING_RATE_LIMIT":
        return f"resume {task_type.lower()} session at retry_after using saved session/checkpoint"
    if status == "WAITING_CI":
        return "recheck CI; auto-merge only if routine and policy-safe"
    if status in {"PENDING", "RETRY"}:
        return f"launch {task_type.lower()} in isolated agent worktree"
    if status == "RUNNING":
        return "persist result/checkpoint before any subsequent assignment"
    if status == "BLOCKED":
        return "repair blocker or explicitly re-queue as a new assignment"
    if status == "DONE" and task_type in {"BUILD", "REPAIR"}:
        return "independent exact-SHA Claude review"
    if status == "DONE" and task_type == "REVIEW":
        return "CI/merge gate or Codex repair according to verdict"
    return "no automatic next step"


class RuntimeLedgerFiles:
    """Structured file projection around the existing SQLite runtime ledger."""

    def __init__(self, root: Path, db_path: Path, repository: str, status_issue: int) -> None:
        self.root = root
        self.db_path = db_path
        self.repository = repository
        self.status_issue = int(status_issue)
        self.events_dir = root / "events"
        self.trello_outbox_dir = root / "trello-outbox"
        self.runs_dir = root / "runs"
        self.checkpoints_dir = root / "checkpoints"
        _mkdir(root, 0o711)
        _mkdir(self.events_dir, 0o700)
        _mkdir(self.trello_outbox_dir, 0o700)
        _mkdir(self.runs_dir, 0o700)
        _mkdir(self.checkpoints_dir, 0o700)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def event(self, kind: str, **payload: Any) -> dict[str, Any]:
        row: dict[str, Any] = {"at": utcnow(), "event": kind}
        for key, value in payload.items():
            if value is None:
                continue
            if isinstance(value, (dict, list, int, float, bool)):
                row[key] = value
            else:
                row[key] = redact(str(value))[:2000]
        path = self.events_dir / f"{row['at'][:10]}.jsonl"
        _mkdir(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
        # Trello is a projection only.  Queue after the canonical material event is
        # durable; a separate root-owned bridge drains these files asynchronously.
        if isinstance(row.get("issue"), int):
            stamp = row["at"].replace(":", "").replace("-", "")
            outbox = self.trello_outbox_dir / f"{stamp}-{os.getpid()}-{time.time_ns()}.json"
            _atomic_json(outbox, {"repository": self.repository, **row})
        return row

    def _task_view(self, task: dict[str, Any] | None) -> dict[str, Any] | None:
        if not task:
            return None
        return {
            "assignment_id": task.get("id"),
            "agent": task.get("agent"),
            "model": task.get("model_class"),
            "role": _role(task),
            "task_type": task.get("task_type"),
            "task_class": task.get("task_class"),
            "issue": task.get("issue_number"),
            "pr": task.get("pr_number"),
            "target_sha": task.get("target_sha"),
            "previous_sha": task.get("previous_sha"),
            "status": task.get("status"),
            "branch": task.get("branch"),
            "worktree": task.get("workdir"),
            "session_id": task.get("session_id"),
            "retry_after": task.get("retry_at"),
            "limit_text": redact(str(task.get("limit_text") or ""))[:1200] or None,
            "systemd_unit": task.get("systemd_unit"),
            "blockers": _safe_blockers(task.get("blockers_json")),
            "blocker": redact(str(task.get("last_error") or ""))[:1200] or None,
            "current_step": _current_step(task),
            "next_step": _next_step(task),
            "updated_at": task.get("updated_at"),
            "issue_url": f"https://github.com/{self.repository}/issues/{task.get('issue_number')}",
            "pr_url": (
                f"https://github.com/{self.repository}/pull/{task.get('pr_number')}"
                if task.get("pr_number")
                else None
            ),
        }

    def checkpoint(
        self,
        task: sqlite3.Row | dict[str, Any],
        *,
        status: str | None = None,
        current_step: str | None = None,
        next_step: str | None = None,
        systemd_unit: str | None = None,
        heartbeat_at: str | None = None,
    ) -> dict[str, Any]:
        data = dict(task)
        if status:
            data["status"] = status
        view = self._task_view(data) or {}
        view["heartbeat_at"] = heartbeat_at or utcnow()
        view["systemd_unit"] = systemd_unit
        if current_step:
            view["current_step"] = current_step
        if next_step:
            view["next_step"] = next_step
        key = _agent_key(str(data.get("agent") or "CODEX"))
        _atomic_json(self.checkpoints_dir / f"{key}.json", view)
        return view

    def run_started(
        self,
        run_id: int,
        task: sqlite3.Row | dict[str, Any],
        *,
        prompt: str,
        systemd_unit: str,
    ) -> None:
        data = dict(task)
        started = utcnow()
        run_dir = self.runs_dir / str(run_id)
        _mkdir(run_dir, 0o700)
        meta = {
            "run_id": run_id,
            "assignment_id": data.get("id"),
            "agent": data.get("agent"),
            "model": data.get("model_class"),
            "role": _role(data),
            "task_type": data.get("task_type"),
            "task_class": data.get("task_class"),
            "issue": data.get("issue_number"),
            "pr": data.get("pr_number"),
            "target_sha": data.get("target_sha"),
            "assignment_source": f"GitHub Issue #{data.get('issue_number')}",
            "status": "RUNNING",
            "started_at": started,
            "heartbeat_at": started,
            "ended_at": None,
            "current_step": _current_step({**data, "status": "RUNNING"}),
            "next_step": "persist result/checkpoint before any subsequent assignment",
            "blocker": None,
            "retry_after": data.get("retry_at"),
            "branch": data.get("branch"),
            "worktree": data.get("workdir"),
            "pid": None,
            "systemd_unit": systemd_unit,
            "session_id": data.get("session_id"),
            "evidence": {
                "issue": f"https://github.com/{self.repository}/issues/{data.get('issue_number')}",
                "pr": (
                    f"https://github.com/{self.repository}/pull/{data.get('pr_number')}"
                    if data.get("pr_number")
                    else None
                ),
            },
        }
        _atomic_json(run_dir / "meta.json", meta)
        _atomic_text(run_dir / "prompt.txt", bounded_redacted(prompt))
        _atomic_text(run_dir / "stdout.log", "")
        _atomic_text(run_dir / "stderr.log", "")
        _atomic_json(run_dir / "result.json", {"status": "RUNNING", "run_id": run_id})
        _atomic_text(
            run_dir / "summary.md",
            f"# AI team run {run_id}\n\nRUNNING {data.get('agent')} {data.get('task_type')} "
            f"for Issue #{data.get('issue_number')}.\n",
        )
        self.checkpoint(
            data,
            status="RUNNING",
            systemd_unit=systemd_unit,
            heartbeat_at=started,
        )
        self.event(
            "RUN_STARTED",
            run_id=run_id,
            assignment_id=data.get("id"),
            agent=data.get("agent"),
            model=data.get("model_class"),
            issue=data.get("issue_number"),
            pr=data.get("pr_number"),
            target_sha=data.get("target_sha"),
            systemd_unit=systemd_unit,
        )

    def run_finished(
        self,
        run_id: int,
        task: sqlite3.Row | dict[str, Any],
        *,
        stdout: str | None,
        stderr: str | None,
        exit_code: int,
        session_id: str | None,
        usage: dict[str, int],
        result: str | None,
        error: str | None,
        status: str,
        retry_after: str | None = None,
        blockers: list[str] | None = None,
    ) -> None:
        data = dict(task)
        run_dir = self.runs_dir / str(run_id)
        _mkdir(run_dir, 0o700)
        ended = utcnow()
        meta_path = run_dir / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            meta = {"run_id": run_id, "assignment_id": data.get("id")}
        meta.update(
            {
                "status": status,
                "heartbeat_at": ended,
                "ended_at": ended,
                "exit_code": exit_code,
                "session_id": session_id,
                "retry_after": retry_after,
                "blocker": redact(error)[:1200] if error else None,
                "current_step": _current_step({**data, "status": status}),
                "next_step": _next_step({**data, "status": status, "retry_at": retry_after}),
                "usage": {k: int(v) for k, v in usage.items() if isinstance(v, int)},
            }
        )
        _atomic_json(meta_path, meta)
        _atomic_text(run_dir / "stdout.log", bounded_redacted(stdout))
        _atomic_text(run_dir / "stderr.log", bounded_redacted(stderr))
        result_payload = {
            "run_id": run_id,
            "status": status,
            "exit_code": exit_code,
            "session_id": session_id,
            "retry_after": retry_after,
            "blockers": [redact(str(x))[:1000] for x in (blockers or [])],
            "result": bounded_redacted(result, 64 * 1024),
            "error": bounded_redacted(error, 16 * 1024),
            "usage": meta.get("usage", {}),
        }
        _atomic_json(run_dir / "result.json", result_payload)
        summary_lines = [
            f"# AI team run {run_id}",
            "",
            f"- agent: {data.get('agent')}",
            f"- model: {data.get('model_class')}",
            f"- task: {data.get('task_type')} / Issue #{data.get('issue_number')}",
            f"- status: {status}",
            f"- target_sha: {data.get('target_sha') or 'NONE'}",
            f"- next: {_next_step({**data, 'status': status, 'retry_at': retry_after})}",
        ]
        if retry_after:
            summary_lines.append(f"- retry_after: {retry_after}")
        if error:
            summary_lines.append(f"- blocker: {redact(error)[:500]}")
        _atomic_text(run_dir / "summary.md", "\n".join(summary_lines) + "\n")
        checkpoint_data = dict(data)
        checkpoint_data["session_id"] = session_id or data.get("session_id")
        checkpoint_data["retry_at"] = retry_after
        checkpoint_data["last_error"] = error
        checkpoint_data["blockers_json"] = json.dumps(
            blockers or _safe_blockers(data.get("blockers_json"))
        )
        self.checkpoint(checkpoint_data, status=status, heartbeat_at=ended)
        self.event(
            "RUN_FINISHED",
            run_id=run_id,
            assignment_id=data.get("id"),
            agent=data.get("agent"),
            model=data.get("model_class"),
            issue=data.get("issue_number"),
            pr=data.get("pr_number"),
            target_sha=data.get("target_sha"),
            status=status,
            retry_after=retry_after,
            exit_code=exit_code,
        )

    def last_events(self, limit: int = 5) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self.events_dir.glob("*.jsonl"), reverse=True):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(rows) >= limit:
                    return list(reversed(rows))
        return list(reversed(rows))

    def project_current(self) -> dict[str, Any]:
        if not self.db_path.exists():
            payload = {
                "version": 1,
                "repository": self.repository,
                "assignment": {"codex": None, "claude": None, "codex_builder": None,
                               "codex_reviewer": None, "sonnet": None, "opus": None},
                "runtime": {"codex": None, "claude": None, "codex_builder": None,
                            "codex_reviewer": None, "sonnet": None, "opus": None},
                "safety": {"real_trading": "NO", "polymarket_scope": "DENIED"},
            }
            _atomic_json(self.root / "current.json", payload)
            return payload
        with self._db() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM tasks ORDER BY created_at ASC"
                ).fetchall()
            ]
            active = [r for r in rows if str(r.get("status")) in ACTIVE]
            assignment: dict[str, Any] = {"codex": None, "claude": None}
            runtime: dict[str, Any] = {"codex": None, "claude": None}
            for key in ("codex", "claude"):
                candidates = [r for r in active if _agent_key(str(r.get("agent") or "")) == key]
                if candidates:
                    assignment[key] = self._task_view(candidates[0])
                running = [r for r in candidates if r.get("status") == "RUNNING"]
                if running:
                    runtime[key] = self._task_view(running[0])
            for slot in ("codex_builder", "codex_reviewer", "sonnet", "opus"):
                candidates = [r for r in active if _slot(r) == slot]
                assignment[slot] = self._task_view(candidates[0]) if candidates else None
                running = [r for r in candidates if r.get("status") == "RUNNING"]
                runtime[slot] = self._task_view(running[0]) if running else None
            last_review_row = db.execute(
                "SELECT * FROM tasks WHERE task_type='REVIEW' ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            last_run = db.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last_ok = db.execute(
                "SELECT ended_at FROM runs WHERE exit_code=0 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        payload = {
            "version": 1,
            "repository": self.repository,
            "assignment": assignment,
            "runtime": runtime,
            "latest_review": self._task_view(_row_dict(last_review_row)),
            "latest_run": _row_dict(last_run),
            "last_successful_run": last_ok["ended_at"] if last_ok else None,
            "last_material_events": self.last_events(5),
            "safety": {"real_trading": "NO", "polymarket_scope": "DENIED"},
        }
        if payload["latest_run"]:
            for key in ("result", "error"):
                if key in payload["latest_run"]:
                    value = payload["latest_run"].get(key)
                    payload["latest_run"][key] = bounded_redacted(value, 1200) if value else None
        _atomic_json(self.root / "current.json", payload)
        for key in ("codex", "claude"):
            if assignment[key]:
                _atomic_json(self.checkpoints_dir / f"{key}.json", assignment[key])
        return payload

    def handoff(
        self,
        *,
        main_head: str,
        active_priorities: list[dict[str, Any]],
        pending_owner_action: str | None = None,
    ) -> str:
        current = self.project_current()
        payload = {
            "protocol": "AI_TEAM_RUNTIME_STATUS_V1",
            "repository": self.repository,
            "main_head": main_head,
            "live_trading": "NO",
            "polymarket_scope": "DENIED",
            "active_p0_p1": active_priorities[:8],
            "codex": current["assignment"].get("codex"),
            "claude": current["assignment"].get("claude"),
            "logical_roles": {key: current["assignment"].get(key) for key in
                              ("codex_builder", "codex_reviewer", "sonnet", "opus")},
            "runtime": current.get("runtime"),
            "latest_review": current.get("latest_review"),
            "last_successful_run": current.get("last_successful_run"),
            "pending_owner_action": pending_owner_action,
            "last_5_material_events": current.get("last_material_events", [])[-5:],
        }
        # Keep the GitHub checkpoint compact and stable. Drop low-value fields before
        # truncating any semantic value.
        def compact_task(task: Any) -> Any:
            if not isinstance(task, dict):
                return task
            keep = (
                "assignment_id",
                "agent",
                "model",
                "role",
                "task_type",
                "task_class",
                "issue",
                "pr",
                "target_sha",
                "previous_sha",
                "status",
                "branch",
                "session_id",
                "retry_after",
                "blocker",
                "current_step",
                "next_step",
                "updated_at",
            )
            return {k: task.get(k) for k in keep if task.get(k) not in (None, "", [])}

        payload["codex"] = compact_task(payload["codex"])
        payload["claude"] = compact_task(payload["claude"])
        payload["logical_roles"] = {
            key: compact_task(value) for key, value in payload["logical_roles"].items()
        }
        if isinstance(payload.get("runtime"), dict):
            payload["runtime"] = {key: compact_task(value)
                                  for key, value in payload["runtime"].items()}
        payload["latest_review"] = compact_task(payload["latest_review"])
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        while len(raw.encode("utf-8")) > 3300 and payload["last_5_material_events"]:
            payload["last_5_material_events"] = payload["last_5_material_events"][1:]
            raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if len(raw.encode("utf-8")) > 3300:
            payload["active_p0_p1"] = payload["active_p0_p1"][:3]
            raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if len(raw.encode("utf-8")) > 3500:
            raise RuntimeError("runtime handoff exceeded compact checkpoint budget")
        body = (
            "<!-- AI_TEAM_RUNTIME_STATUS_V1 -->\n"
            "# AI TEAM RUNTIME STATUS\n\n"
            "Canonical runtime handoff for #129. GitHub remains canonical for accepted "
            "code/tasks/reviews; "
            "the VM ledger is canonical for transient execution.\n\n"
            "```json\n"
            + raw
            + "\n```\n\n"
            "Fresh-session bootstrap: read this checkpoint and `AGENTS.md`, verify GitHub "
            "state, then continue "
            "the listed next step. Do not rely on previous chat history.\n"
        )
        if len(body.encode("utf-8")) > MAX_HANDOFF_BYTES:
            raise RuntimeError("runtime status Issue body exceeds 4 KB target")
        _atomic_text(self.root / "handoff.md", body)
        return body
