#!/usr/bin/env python3
"""Small GitHub-canonical orchestrator for the Hyperliquid AI engineering team.

Security model:
- root orchestrator owns GitHub/git push/merge authority;
- Codex and Claude run as separate non-root system users with no GitHub credentials;
- model processes run in transient systemd sandboxes with /root and /mnt inaccessible;
- the orchestrator only operates on the configured Hyperliquid repository and its own state roots;
- real-trading enablement and live-sensitive paths fail closed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_team_runtime_ledger import RuntimeLedgerFiles, bounded_redacted

REPO = "aliezzat4321/hyperliquid-copy-engine"
STATE_ROOT = Path("/var/lib/hyperliquid-ai-team")
DB_PATH = STATE_ROOT / "orchestrator" / "ledger.sqlite3"
LOCK_PATH = Path("/run/hyperliquid-ai-team/orchestrator.lock")
CONFIG_PATHS = [
    Path("/etc/hyperliquid-ai-team/router.json"),
    Path("/opt/hyperliquid-ai-team/config/ai_team_router.json"),
    Path(__file__).resolve().parents[1] / "config" / "ai_team_router.json",
]
CODEX_USER = "hl-codex-agent"
CLAUDE_USER = "hl-claude-agent"
CODEX_HOME = STATE_ROOT / "agents" / "codex" / "home"
CLAUDE_HOME = STATE_ROOT / "agents" / "claude" / "home"
CODEX_WORK = STATE_ROOT / "agents" / "codex" / "worktrees"
CLAUDE_WORK = STATE_ROOT / "agents" / "claude" / "worktrees"
CODEX_LOG = STATE_ROOT / "agents" / "codex" / "logs"
CLAUDE_LOG = STATE_ROOT / "agents" / "claude" / "logs"
CLAUDE_ENV_FILE = Path("/etc/hyperliquid-ai-team/claude.env")
CLAUDE_CREDENTIALS = CLAUDE_HOME / ".claude" / ".credentials.json"
RUNTIME_STATUS_ISSUE = 130
GIT_PUSH_REMOTE = f"git@github.com:{REPO}.git"
MACHINE_ASSIGNMENT = "AI_TEAM_ASSIGNMENT_V1"
MACHINE_RESULT = "AI_TEAM_RESULT_V1"
ACTIVE_STATUSES = {"PENDING", "RETRY", "WAITING_RATE_LIMIT", "WAITING_CI", "RUNNING"}
TERMINAL_STATUSES = {"DONE", "FAILED", "BLOCKED", "STALE"}

DEFAULT_CONFIG: dict[str, Any] = {
    "protocol_version": 1,
    "repository": REPO,
    "routing": {
        "BUILD": {"agent": "CODEX_CHATGPT", "model_class": "CODEX_DEFAULT"},
        "REPAIR": {"agent": "CODEX_CHATGPT", "model_class": "CODEX_DEFAULT"},
        "REVIEW": {"agent": "CLAUDE", "model_class": "SONNET"},
        "RESEARCH": {"agent": "CLAUDE", "model_class": "SONNET"},
    },
    "opus_allowed_task_classes": [
        "QUANT_PROFITABILITY",
        "STATISTICAL_METHODOLOGY",
        "MAJOR_ARCHITECTURE",
        "UNRESOLVED_DISAGREEMENT",
        "CAPITAL_SENSITIVE_METHODOLOGY",
    ],
    "opus_allowed_reasons": [
        "QUANT_PROFITABILITY",
        "STATISTICAL_METHODOLOGY",
        "MAJOR_ARCHITECTURE",
        "UNRESOLVED_DISAGREEMENT",
        "CAPITAL_SENSITIVE_METHODOLOGY",
    ],
    "auto_merge_task_classes": ["ROUTINE"],
    "max_attempts": 3,
    "poll_seconds": 60,
    "default_rate_limit_retry_seconds": 3600,
    "review_timeout_seconds": 1200,
    "build_timeout_seconds": 1800,
    "trusted_author_associations": ["OWNER", "MEMBER", "COLLABORATOR"],
    "labels": {
        "ready": "ai-team:ready",
        "pending": "ai-team:pending",
        "running": "ai-team:running",
        "waiting_review": "ai-team:waiting-review",
        "blocked": "ai-team:blocked",
        "done": "ai-team:done",
    },
    "safety": {
        "real_trading_required_value": "NO",
        "forbidden_enable_patterns": [
            r"REAL_TRADING_ENABLED\s*=\s*(YES|TRUE|1)",
            r"LIVE_TRADING_ENABLED\s*=\s*(YES|TRUE|1)",
        ],
        "no_auto_merge_path_prefixes": [
            "src/hlcopy/trading/",
            "docs/ai-team/LIVE_TRADING_GATE.md",
            "services/invo-notification-executor/src/hl-client.ts",
        ],
        "agent_hidden_paths": ["/root", "/mnt"],
    },
}


def utcnow() -> str:
    now = dt.datetime.now(dt.timezone.utc)
    return now.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_config() -> dict[str, Any]:
    for path in CONFIG_PATHS:
        if path.exists():
            cfg = json.loads(path.read_text())
            if cfg.get("repository") != REPO:
                raise RuntimeError(f"router repository mismatch: {cfg.get('repository')}")
            return cfg
    return DEFAULT_CONFIG


def run(
    cmd: list[str],
    *,
    input_text: str | None = None,
    cwd: Path | None = None,
    timeout: int | None = None,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        env=env,
        check=False,
    )
    if check and cp.returncode != 0:
        raise RuntimeError(
            f"command failed rc={cp.returncode}: {shlex.join(cmd)}\n"
            f"stdout={cp.stdout[-2000:]}\nstderr={cp.stderr[-2000:]}"
        )
    return cp


def git_worktree(
    workdir: Path, *args: str, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run root-side Git only with this generated worktree explicitly trusted."""
    return run(
        [
            "git",
            "-c",
            f"safe.directory={workdir}",
            "-C",
            str(workdir),
            *args,
        ],
        check=check,
    )


class GitHub:
    def __init__(self, repo: str) -> None:
        if repo != REPO:
            raise RuntimeError("refusing non-Hyperliquid repository")
        self.repo = repo

    def api(self, method: str, path: str, payload: Any | None = None) -> Any:
        cmd = ["gh", "api", "--method", method.upper(), path]
        text = None
        if payload is not None:
            cmd += ["--input", "-"]
            text = json.dumps(payload, separators=(",", ":"))
        cp = run(cmd, input_text=text, timeout=60)
        if cp.returncode != 0:
            raise RuntimeError(
                f"GitHub unavailable/error: {cp.stderr[-1500:] or cp.stdout[-1500:]}"
            )
        if not cp.stdout.strip():
            return None
        return json.loads(cp.stdout)

    def issue(self, number: int) -> dict[str, Any]:
        return self.api("GET", f"repos/{self.repo}/issues/{number}")

    def comments(self, number: int) -> list[dict[str, Any]]:
        return self.api("GET", f"repos/{self.repo}/issues/{number}/comments?per_page=100") or []

    def comment(self, number: int, body: str) -> dict[str, Any]:
        return self.api("POST", f"repos/{self.repo}/issues/{number}/comments", {"body": body})

    def ready_issues(self, label: str) -> list[dict[str, Any]]:
        q = urllib.parse.quote(label, safe="")
        rows = self.api("GET", f"repos/{self.repo}/issues?state=open&labels={q}&per_page=30") or []
        return [row for row in rows if "pull_request" not in row]

    def add_labels(self, number: int, labels: list[str]) -> None:
        if labels:
            self.api("POST", f"repos/{self.repo}/issues/{number}/labels", {"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        enc = urllib.parse.quote(label, safe="")
        cp = run(
            ["gh", "api", "--method", "DELETE", f"repos/{self.repo}/issues/{number}/labels/{enc}"]
        )
        if cp.returncode != 0 and "404" not in (cp.stderr + cp.stdout):
            raise RuntimeError(f"remove label failed: {cp.stderr[-800:]}")

    def create_pr(self, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        return self.api(
            "POST",
            f"repos/{self.repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
        )

    def pr(self, number: int) -> dict[str, Any]:
        return self.api("GET", f"repos/{self.repo}/pulls/{number}")

    def changed_files(self, pr_number: int) -> list[str]:
        rows = self.api("GET", f"repos/{self.repo}/pulls/{pr_number}/files?per_page=100") or []
        return [str(row["filename"]) for row in rows]

    def check_state(self, sha: str) -> tuple[str, str]:
        checks = (
            self.api(
                "GET",
                f"repos/{self.repo}/commits/{sha}/check-runs?per_page=100",
            )
            or {}
        )
        runs = checks.get("check_runs", [])
        statuses = self.api("GET", f"repos/{self.repo}/commits/{sha}/status") or {}
        if not runs:
            return "PENDING", "no check-runs visible yet"
        pending = [x for x in runs if x.get("status") != "completed"]
        if pending:
            return "PENDING", ", ".join(str(x.get("name")) for x in pending[:6])
        bad = [x for x in runs if x.get("conclusion") not in {"success", "neutral", "skipped"}]
        if bad:
            return "FAIL", ", ".join(f"{x.get('name')}={x.get('conclusion')}" for x in bad[:6])
        if statuses.get("total_count", 0) and statuses.get("state") != "success":
            return "PENDING" if statuses.get("state") == "pending" else "FAIL", (
                f"combined status={statuses.get('state')}"
            )
        return "PASS", f"{len(runs)} check-runs green"

    def merge(self, pr_number: int, sha: str) -> dict[str, Any]:
        return self.api(
            "PUT",
            f"repos/{self.repo}/pulls/{pr_number}/merge",
            {"merge_method": "squash", "sha": sha},
        )

    def close_issue(self, number: int) -> None:
        self.api("PATCH", f"repos/{self.repo}/issues/{number}", {"state": "closed"})


class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              issue_number INTEGER NOT NULL,
              pr_number INTEGER,
              task_type TEXT NOT NULL,
              agent TEXT NOT NULL,
              model_class TEXT NOT NULL,
              task_class TEXT NOT NULL,
              status TEXT NOT NULL,
              branch TEXT,
              target_sha TEXT,
              previous_sha TEXT,
              blockers_json TEXT,
              workdir TEXT,
              session_id TEXT,
              attempt INTEGER NOT NULL DEFAULT 0,
              retry_at TEXT,
              last_error TEXT,
              parent_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tasks_due_idx ON tasks(status, retry_at, created_at);
            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id TEXT NOT NULL,
              agent TEXT NOT NULL,
              model_class TEXT NOT NULL,
              started_at TEXT NOT NULL,
              ended_at TEXT,
              exit_code INTEGER,
              session_id TEXT,
              input_tokens INTEGER,
              output_tokens INTEGER,
              cached_input_tokens INTEGER,
              log_path TEXT,
              result TEXT,
              error TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    def recover_interrupted(self) -> None:
        now = utcnow()
        self.db.execute(
            """
            UPDATE tasks
               SET status='RETRY',
                   retry_at=?,
                   last_error=COALESCE(last_error,'orchestrator restarted during task'),
                   updated_at=?
             WHERE status='RUNNING'
            """,
            (now, now),
        )
        self.db.commit()

    def create_task(self, **kw: Any) -> str:
        task_id = kw.get("id") or uuid.uuid4().hex[:16]
        now = utcnow()
        fields = {
            "id": task_id,
            "issue_number": kw["issue_number"],
            "pr_number": kw.get("pr_number"),
            "task_type": kw["task_type"],
            "agent": kw["agent"],
            "model_class": kw["model_class"],
            "task_class": kw.get("task_class", "ROUTINE"),
            "status": kw.get("status", "PENDING"),
            "branch": kw.get("branch"),
            "target_sha": kw.get("target_sha"),
            "previous_sha": kw.get("previous_sha"),
            "blockers_json": json.dumps(kw.get("blockers") or []),
            "workdir": kw.get("workdir"),
            "session_id": kw.get("session_id"),
            "attempt": int(kw.get("attempt", 0)),
            "retry_at": kw.get("retry_at"),
            "last_error": kw.get("last_error"),
            "parent_id": kw.get("parent_id"),
            "created_at": now,
            "updated_at": now,
        }
        cols = ",".join(fields)
        q = ",".join("?" for _ in fields)
        self.db.execute(
            f"INSERT INTO tasks ({cols}) VALUES ({q})",
            list(fields.values()),
        )
        self.db.commit()
        return task_id

    def get(self, task_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(task_id)
        return row

    def update(self, task_id: str, **kw: Any) -> None:
        if not kw:
            return
        kw["updated_at"] = utcnow()
        sets = ",".join(f"{k}=?" for k in kw)
        self.db.execute(
            f"UPDATE tasks SET {sets} WHERE id=?",
            [*kw.values(), task_id],
        )
        self.db.commit()

    def due(self) -> sqlite3.Row | None:
        now = utcnow()
        return self.db.execute(
            """
            SELECT *
              FROM tasks
             WHERE status IN ('PENDING','RETRY','WAITING_RATE_LIMIT','WAITING_CI')
               AND (retry_at IS NULL OR retry_at <= ?)
             ORDER BY CASE status WHEN 'WAITING_CI' THEN 0 ELSE 1 END, created_at
             LIMIT 1
            """,
            (now,),
        ).fetchone()

    def active_for_issue(self, issue_number: int) -> bool:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = self.db.execute(
            f"SELECT 1 FROM tasks WHERE issue_number=? AND status IN ({placeholders}) LIMIT 1",
            (issue_number, *ACTIVE_STATUSES),
        ).fetchone()
        return bool(row)

    def open_run(self, task: sqlite3.Row, log_path: Path) -> int:
        cur = self.db.execute(
            """
            INSERT INTO runs(task_id,agent,model_class,started_at,log_path)
            VALUES(?,?,?,?,?)
            """,
            (task["id"], task["agent"], task["model_class"], utcnow(), str(log_path)),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def close_run(
        self,
        run_id: int,
        *,
        exit_code: int,
        session_id: str | None,
        usage: dict[str, int],
        result: str | None,
        error: str | None,
    ) -> None:
        self.db.execute(
            """
            UPDATE runs
               SET ended_at=?, exit_code=?, session_id=?,
                   input_tokens=?, output_tokens=?, cached_input_tokens=?,
                   result=?, error=?
             WHERE id=?
            """,
            (
                utcnow(),
                exit_code,
                session_id,
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                usage.get("cached_input_tokens"),
                result,
                error,
                run_id,
            ),
        )
        self.db.commit()

    def status_snapshot(self) -> dict[str, Any]:
        current = self.db.execute(
            """
            SELECT * FROM tasks
             WHERE status IN ('RUNNING','PENDING','RETRY','WAITING_RATE_LIMIT','WAITING_CI')
             ORDER BY CASE status WHEN 'RUNNING' THEN 0 ELSE 1 END, created_at
             LIMIT 1
            """
        ).fetchone()
        last_review = self.db.execute(
            "SELECT * FROM tasks WHERE task_type='REVIEW' "
            "AND status IN ('DONE','BLOCKED','FAILED') "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        last_success = self.db.execute(
            "SELECT ended_at FROM runs WHERE exit_code=0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        failures = self.db.execute(
            "SELECT task_id,agent,error,ended_at FROM runs WHERE exit_code!=0 "
            "ORDER BY id DESC LIMIT 5"
        ).fetchall()
        return {
            "current": dict(current) if current else None,
            "last_review": dict(last_review) if last_review else None,
            "last_success": last_success["ended_at"] if last_success else None,
            "failures": [dict(x) for x in failures],
        }


def parse_task_class(body: str) -> tuple[str, str | None]:
    allowed = {
        "ROUTINE",
        "QUANT_PROFITABILITY",
        "STATISTICAL_METHODOLOGY",
        "MAJOR_ARCHITECTURE",
        "UNRESOLVED_DISAGREEMENT",
        "CAPITAL_SENSITIVE_METHODOLOGY",
    }
    m = re.search(r"(?mi)^\s*(?:AI_)?TASK_CLASS\s*=\s*([A-Z_]+)\s*$", body or "")
    task_class = m.group(1) if m and m.group(1) in allowed else "ROUTINE"
    e = re.search(r"(?mi)^\s*OPUS_ESCALATION_REASON\s*=\s*([A-Z_]+)\s*$", body or "")
    return task_class, e.group(1) if e else None


def route_review(cfg: dict[str, Any], task_class: str, escalation_reason: str | None) -> str:
    if task_class in cfg["opus_allowed_task_classes"]:
        if escalation_reason and escalation_reason not in cfg["opus_allowed_reasons"]:
            raise RuntimeError(f"invalid Opus escalation reason: {escalation_reason}")
        return "OPUS"
    if escalation_reason:
        raise RuntimeError("Opus escalation reason supplied for non-Opus task class")
    return "SONNET"


def assignment_marker(
    *,
    task_id: str,
    agent: str,
    task_type: str,
    model_class: str,
    task_class: str,
    issue_number: int,
    pr_number: int | None = None,
    target_sha: str | None = None,
    parent_id: str | None = None,
    previous_sha: str | None = None,
    escalation_reason: str | None = None,
) -> str:
    fields = [
        "AI_TEAM_PROTOCOL=1",
        f"ASSIGNMENT_ID={task_id}",
        f"ASSIGNED_AGENT={agent}",
        f"TASK_TYPE={task_type}",
        f"MODEL_CLASS={model_class}",
        f"TASK_CLASS={task_class}",
        f"TARGET_ISSUE={issue_number}",
        f"TARGET_PR={pr_number or ''}",
        f"TARGET_SHA={target_sha or ''}",
        "STATUS=PENDING",
        f"PARENT_ASSIGNMENT_ID={parent_id or ''}",
        f"PREVIOUS_REVIEWED_SHA={previous_sha or ''}",
        f"ESCALATION_REASON={escalation_reason or ''}",
    ]
    return f"<!-- {MACHINE_ASSIGNMENT}\n" + "\n".join(fields) + "\n-->"


def result_marker(
    *,
    task_id: str,
    reviewed_sha: str,
    verdict: str,
    reviewer: str,
    model_class: str,
    blockers: list[str],
    summary: str,
) -> str:
    safe_summary = " ".join(summary.split())[:600]
    fields = [
        "AI_TEAM_PROTOCOL=1",
        f"ASSIGNMENT_ID={task_id}",
        f"REVIEWED_SHA={reviewed_sha}",
        f"VERDICT={verdict}",
        f"REVIEWER={reviewer}",
        f"MODEL_CLASS={model_class}",
        f"BLOCKERS_JSON={json.dumps(blockers, separators=(',', ':'))}",
        f"SUMMARY={safe_summary}",
    ]
    return f"<!-- {MACHINE_RESULT}\n" + "\n".join(fields) + "\n-->"


def recent_human_comments(gh: GitHub, number: int, trusted: set[str], limit: int = 8) -> list[str]:
    rows = []
    for c in gh.comments(number):
        body = str(c.get("body") or "")
        if MACHINE_ASSIGNMENT in body or MACHINE_RESULT in body:
            continue
        if str(c.get("author_association") or "") not in trusted:
            continue
        body = body.strip()
        if body:
            rows.append(body[:4000])
    return rows[-limit:]


def prepare_checkout(
    *, user: str, home: Path, base_dir: Path, task_id: str, ref: str, branch: str | None
) -> Path:
    workdir = base_dir / task_id
    if workdir.exists():
        return workdir
    workdir.parent.mkdir(parents=True, exist_ok=True)
    cp = run(
        ["git", "clone", "--quiet", f"https://github.com/{REPO}.git", str(workdir)], timeout=180
    )
    if cp.returncode != 0:
        raise RuntimeError(f"clone failed: {cp.stderr[-1200:]}")
    run(["git", "-C", str(workdir), "checkout", "--quiet", ref], timeout=60, check=True)
    if branch:
        run(["git", "-C", str(workdir), "checkout", "-B", branch], timeout=60, check=True)
    uid = int(run(["id", "-u", user], check=True).stdout.strip())
    gid = int(run(["id", "-g", user], check=True).stdout.strip())
    os.chown(workdir, uid, gid)
    for root, dirs, files in os.walk(workdir):
        for name in dirs:
            os.chown(Path(root) / name, uid, gid)
        for name in files:
            os.chown(Path(root) / name, uid, gid)
    run(
        [
            "setpriv",
            f"--reuid={uid}",
            f"--regid={gid}",
            "--init-groups",
            "git",
            "-C",
            str(workdir),
            "config",
            "user.name",
            "AI Team",
        ],
        check=True,
    )
    run(
        [
            "setpriv",
            f"--reuid={uid}",
            f"--regid={gid}",
            "--init-groups",
            "git",
            "-C",
            str(workdir),
            "config",
            "user.email",
            "ai-team@localhost",
        ],
        check=True,
    )
    return workdir


def model_sandbox_command(
    *,
    unit: str,
    user: str,
    home: Path,
    workdir: Path,
    command: list[str],
    env_file: Path | None = None,
) -> list[str]:
    args = [
        "systemd-run",
        "--pipe",
        "--wait",
        "--collect",
        "--quiet",
        f"--unit={unit}",
        f"--uid={user}",
        f"--gid={user}",
        f"--working-directory={workdir}",
        "--property=NoNewPrivileges=yes",
        "--property=PrivateTmp=yes",
        "--property=ProtectHome=yes",
        "--property=ProtectSystem=strict",
        "--property=RestrictSUIDSGID=yes",
        "--property=InaccessiblePaths=/mnt",
        f"--property=ReadWritePaths={workdir} {home}",
        f"--setenv=HOME={home}",
    ]
    if user == CODEX_USER:
        args += [f"--setenv=CODEX_HOME={home / '.codex'}"]
    else:
        args += [f"--setenv=CLAUDE_CONFIG_DIR={home / '.claude'}"]
    if env_file is not None:
        args += [f"--property=EnvironmentFile={env_file}"]
    return [*args, *command]


def codex_runtime_preflight(
    codex_path: Path = Path("/usr/local/bin/codex"),
    bwrap_path: Path = Path("/usr/local/bin/bwrap"),
) -> Path:
    """Refuse a model call if Codex or its Linux sandbox dependencies are missing."""
    if not codex_path.is_file() or not os.access(codex_path, os.X_OK):
        raise RuntimeError(f"Codex CLI missing or not executable: {codex_path}")
    host = codex_path.with_name("codex-code-mode-host")
    if not host.is_file() or not os.access(host, os.X_OK):
        raise RuntimeError(
            "Codex Code Mode host missing or not executable; refusing model call: "
            f"{host}"
        )
    if not bwrap_path.is_file() or not os.access(bwrap_path, os.X_OK):
        raise RuntimeError(
            f"Codex Linux workspace sandbox dependency missing: {bwrap_path}"
        )
    return host


def claude_runtime_preflight(
    claude_path: Path = Path("/usr/bin/claude"),
    credentials: Path = CLAUDE_CREDENTIALS,
) -> Path:
    """Use the isolated Claude subscription OAuth credential, never a copied setup-token."""
    if not claude_path.is_file() or not os.access(claude_path, os.X_OK):
        raise RuntimeError(f"Claude CLI missing or not executable: {claude_path}")
    if not credentials.is_file():
        raise RuntimeError(f"Claude subscription credential missing: {credentials}")
    return credentials


def acceptance_flag(body: str, name: str) -> bool:
    return bool(re.search(rf"(?mi)^\s*{re.escape(name)}\s*=\s*YES\s*$", body))


def retry_at_after(seconds: int) -> str:
    value = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_codex_stream(text: str) -> tuple[str | None, dict[str, int], str]:
    session_id = None
    usage: dict[str, int] = {}
    result = ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") == "thread.started":
            session_id = row.get("thread_id") or row.get("thread", {}).get("id")
        if row.get("type") == "turn.completed":
            u = row.get("usage") or row.get("turn", {}).get("usage") or {}
            for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
                if isinstance(u.get(key), int):
                    usage[key] = int(u[key])
        item = row.get("item") or {}
        if item.get("type") in {"agent_message", "message"} and item.get("text"):
            result = str(item["text"])
        if row.get("type") in {"message.completed", "response.completed"}:
            maybe = row.get("message") or row.get("response") or {}
            if isinstance(maybe, dict) and maybe.get("text"):
                result = str(maybe["text"])
    return session_id, usage, result


def parse_claude_output(text: str) -> tuple[str | None, dict[str, int], str]:
    usage: dict[str, int] = {}
    session_id = None
    result = ""
    candidates = [line for line in text.splitlines() if line.strip().startswith("{")]
    for line in reversed(candidates):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = row.get("session_id") or session_id
        u = row.get("usage") or {}
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
            if isinstance(u.get(key), int):
                target = "cached_input_tokens" if key == "cache_read_input_tokens" else key
                usage[target] = int(u[key])
        if isinstance(row.get("result"), str):
            result = row["result"]
            break
    if not result:
        result = text[-8000:]
    return session_id, usage, result


def rate_limit_info(text: str, default_seconds: int) -> tuple[bool, str | None]:
    low = text.lower()
    phrases = ("rate limit", "usage limit", "quota exceeded", "too many requests", "limit reached")
    if not any(p in low for p in phrases):
        return False, None
    now = dt.datetime.now(dt.timezone.utc)
    m = re.search(r"(?:try again|reset(?:s)?)(?: in)?\s+(\d+)\s*(minute|hour|second)s?", low)
    if m:
        n = int(m.group(1))
        mult = {"second": 1, "minute": 60, "hour": 3600}[m.group(2)]
        when = now + dt.timedelta(seconds=n * mult + 30)
    else:
        when = now + dt.timedelta(seconds=default_seconds)
    return True, when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def extract_review(result: str, target_sha: str) -> tuple[str, list[str], str]:
    verdict_m = re.search(r"(?mi)^\s*VERDICT\s*=\s*(PASS|FAIL)\s*$", result)
    sha_m = re.search(r"(?mi)^\s*REVIEWED_SHA\s*=\s*([0-9a-f]{40})\s*$", result)
    if not verdict_m or not sha_m:
        raise RuntimeError("reviewer did not emit required REVIEWED_SHA/VERDICT lines")
    if sha_m.group(1) != target_sha:
        raise RuntimeError(f"stale reviewer SHA {sha_m.group(1)} != {target_sha}")
    verdict = verdict_m.group(1)
    blockers: list[str] = []
    b = re.search(r"(?mi)^\s*BLOCKERS_JSON\s*=\s*(\[.*\])\s*$", result)
    if b:
        try:
            raw = json.loads(b.group(1))
            if isinstance(raw, list):
                blockers = [str(x)[:1000] for x in raw]
        except json.JSONDecodeError:
            pass
    if verdict == "FAIL" and not blockers:
        blockers = ["Reviewer returned FAIL; see full review comment/log for details."]
    summary = re.sub(r"\s+", " ", result).strip()[:700]
    return verdict, blockers, summary


def changed_files(workdir: Path, base_sha: str) -> list[str]:
    cp = git_worktree(workdir, "diff", "--name-only", base_sha, "--", check=True)
    return [x.strip() for x in cp.stdout.splitlines() if x.strip()]


def validate_changes(cfg: dict[str, Any], workdir: Path, base_sha: str) -> tuple[list[str], bool]:
    files = changed_files(workdir, base_sha)
    if not files:
        raise RuntimeError("agent produced no file changes")
    no_auto = False
    for name in files:
        if name.startswith("/") or ".." in Path(name).parts:
            raise RuntimeError(f"unsafe changed path: {name}")
        if any(name.startswith(p) for p in cfg["safety"]["no_auto_merge_path_prefixes"]):
            raise RuntimeError(f"autonomous task touched owner-sensitive live path: {name}")
    diff = git_worktree(workdir, "diff", base_sha, "--", check=True).stdout
    for pat in cfg["safety"]["forbidden_enable_patterns"]:
        if re.search(pat, diff, flags=re.I):
            raise RuntimeError(f"forbidden live-trading enablement pattern detected: {pat}")
    return files, no_auto


class Orchestrator:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.gh = GitHub(REPO)
        self.ledger = Ledger(DB_PATH)
        self.runtime = RuntimeLedgerFiles(STATE_ROOT, DB_PATH, REPO, RUNTIME_STATUS_ISSUE)
        self.trusted = set(self.cfg["trusted_author_associations"])

    def sync_runtime_checkpoint(self) -> None:
        """Project SQLite runtime state and mirror a compact chat-independent handoff."""
        try:
            main = self.gh.api("GET", f"repos/{REPO}/commits/main") or {}
            rows = self.gh.api("GET", f"repos/{REPO}/issues?state=open&per_page=100") or []
            priorities = []
            for row in rows:
                if "pull_request" in row:
                    continue
                title = str(row.get("title") or "")
                if not title.upper().startswith(("P0", "P1")):
                    continue
                priorities.append({"issue": int(row["number"]), "title": title[:160]})
            snap = self.ledger.status_snapshot()
            cur = snap.get("current") or {}
            pending_owner_action = None
            if "AUTH_REQUIRED" in str(cur.get("last_error") or ""):
                pending_owner_action = str(cur.get("last_error"))[:500]
            body = self.runtime.handoff(
                main_head=str(main.get("sha") or "UNKNOWN"),
                active_priorities=priorities,
                pending_owner_action=pending_owner_action,
            )
            status_issue = self.gh.issue(RUNTIME_STATUS_ISSUE)
            if str(status_issue.get("body") or "") != body:
                self.gh.api(
                    "PATCH",
                    f"repos/{REPO}/issues/{RUNTIME_STATUS_ISSUE}",
                    {"body": body},
                )
        except Exception as exc:
            self.runtime.event("CHECKPOINT_MIRROR_FAILED", error=str(exc))

    def finish_runtime_run(
        self,
        run_id: int,
        task_id: str,
        *,
        stdout: str | None,
        stderr: str | None,
        exit_code: int,
        session_id: str | None,
        usage: dict[str, int],
        result: str | None,
        error: str | None = None,
        status: str | None = None,
        blockers: list[str] | None = None,
    ) -> None:
        final = self.ledger.get(task_id)
        final_error = error if error is not None else final["last_error"]
        self.runtime.run_finished(
            run_id,
            final,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            session_id=session_id,
            usage=usage,
            result=result,
            error=final_error,
            status=status or str(final["status"]),
            retry_after=final["retry_at"],
            blockers=blockers,
        )
        self.sync_runtime_checkpoint()

    def claim_ready_issue(self) -> bool:
        label = self.cfg["labels"]["ready"]
        for issue in self.gh.ready_issues(label):
            number = int(issue["number"])
            if self.ledger.active_for_issue(number):
                continue
            if str(issue.get("author_association") or "") not in self.trusted:
                continue
            body = str(issue.get("body") or "")
            task_class, escalation_reason = parse_task_class(body)
            task_id = self.ledger.create_task(
                issue_number=number,
                task_type="BUILD",
                agent="CODEX_CHATGPT",
                model_class="CODEX_DEFAULT",
                task_class=task_class,
            )
            self.gh.comment(
                number,
                assignment_marker(
                    task_id=task_id,
                    agent="CODEX_CHATGPT",
                    task_type="BUILD",
                    model_class="CODEX_DEFAULT",
                    task_class=task_class,
                    issue_number=number,
                    escalation_reason=escalation_reason,
                ),
            )
            self.gh.add_labels(number, [self.cfg["labels"]["pending"]])
            self.gh.remove_label(number, label)
            self.runtime.event(
                "TASK_ASSIGNED",
                assignment_id=task_id,
                issue=number,
                agent="CODEX_CHATGPT",
                task_type="BUILD",
            )
            self.sync_runtime_checkpoint()
            return True
        return False

    def cycle(self) -> None:
        self.ledger.recover_interrupted()
        self.sync_runtime_checkpoint()
        task = self.ledger.due()
        if task is None:
            self.claim_ready_issue()
            task = self.ledger.due()
        if task is None:
            return
        try:
            if task["status"] == "WAITING_CI":
                self.handle_ci(task)
            elif task["task_type"] in {"BUILD", "REPAIR"}:
                self.handle_codex(task)
            elif task["task_type"] == "REVIEW":
                self.handle_review(task)
            else:
                self.block(task, f"unsupported task type {task['task_type']}")
        finally:
            self.sync_runtime_checkpoint()

    def handle_codex(self, task: sqlite3.Row) -> None:
        issue = self.gh.issue(int(task["issue_number"]))
        if str(issue.get("author_association") or "") not in self.trusted:
            self.block(task, "issue author association no longer trusted")
            return
        try:
            codex_runtime_preflight()
        except RuntimeError as exc:
            self.block(task, f"CODEX_RUNTIME_PREFLIGHT: {exc}")
            return
        if task["task_type"] == "BUILD":
            base_ref = "origin/main"
            branch = task["branch"] or f"codex/auto-{task['issue_number']}-{task['id'][:8]}"
        else:
            if not task["pr_number"]:
                self.block(task, "repair missing PR")
                return
            pr = self.gh.pr(int(task["pr_number"]))
            branch = str(pr["head"]["ref"])
            base_ref = branch
        workdir = (
            Path(task["workdir"])
            if task["workdir"]
            else prepare_checkout(
                user=CODEX_USER,
                home=CODEX_HOME,
                base_dir=CODEX_WORK,
                task_id=str(task["id"]),
                ref=base_ref,
                branch=branch,
            )
        )
        self.ledger.update(
            task["id"],
            status="RUNNING",
            branch=branch,
            workdir=str(workdir),
            attempt=int(task["attempt"]) + 1,
        )
        task = self.ledger.get(task["id"])
        base_sha = git_worktree(workdir, "rev-parse", "HEAD", check=True).stdout.strip()
        comments = recent_human_comments(self.gh, int(task["issue_number"]), self.trusted)
        blockers = json.loads(task["blockers_json"] or "[]")
        prompt = self.codex_prompt(issue, task, comments, blockers)
        log_path = CODEX_LOG / f"{task['id']}-attempt-{task['attempt']}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        run_id = self.ledger.open_run(task, log_path)
        unit = f"hl-ai-codex-{task['id'][:10]}-{int(time.time())}"
        self.runtime.run_started(run_id, task, prompt=prompt, systemd_unit=unit)
        self.sync_runtime_checkpoint()
        try:
            cp = self.invoke_codex(task, workdir, prompt, unit)
        except subprocess.TimeoutExpired as exc:
            text = (exc.stdout or "") + "\n" + (exc.stderr or "")
            session_id, usage, result = parse_codex_stream(text)
            log_path.write_text(bounded_redacted(text))
            self.ledger.close_run(
                run_id,
                exit_code=124,
                session_id=session_id,
                usage=usage,
                result=result,
                error="Codex timeout",
            )
            self.retry_or_block(task, "Codex timeout", session_id=session_id, rate_limited=False)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                exit_code=124,
                session_id=session_id,
                usage=usage,
                result=result,
                error="Codex timeout",
            )
            return
        combined = cp.stdout + ("\n" + cp.stderr if cp.stderr else "")
        log_path.write_text(bounded_redacted(combined))
        session_id, usage, result = parse_codex_stream(cp.stdout)
        limited, retry_at = rate_limit_info(
            combined, int(self.cfg["default_rate_limit_retry_seconds"])
        )
        self.ledger.close_run(
            run_id,
            exit_code=cp.returncode,
            session_id=session_id,
            usage=usage,
            result=result,
            error=None if cp.returncode == 0 else combined[-1500:],
        )
        if cp.returncode != 0:
            if limited:
                self.ledger.update(
                    task["id"],
                    status="WAITING_RATE_LIMIT",
                    retry_at=retry_at,
                    session_id=session_id,
                    last_error="Codex rate/usage limit",
                )
                self.finish_runtime_run(
                    run_id,
                    str(task["id"]),
                    stdout=cp.stdout,
                    stderr=cp.stderr,
                    exit_code=cp.returncode,
                    session_id=session_id,
                    usage=usage,
                    result=result,
                    error="Codex rate/usage limit",
                )
                return
            self.retry_or_block(task, f"Codex failed rc={cp.returncode}", session_id=session_id)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=cp.returncode,
                session_id=session_id,
                usage=usage,
                result=result,
            )
            return
        if (
            acceptance_flag(str(issue.get("body") or ""), "AI_TEAM_TEST_CODEX_INTERRUPT_ONCE")
            and int(task["attempt"]) == 1
        ):
            retry_at = retry_at_after(300)
            self.ledger.update(
                task["id"],
                status="RETRY",
                retry_at=retry_at,
                session_id=session_id,
                last_error="TEST_INJECTED_CODEX_SESSION_END",
            )
            self.runtime.event(
                "TEST_CODEX_SESSION_INTERRUPTED",
                assignment_id=task["id"],
                issue=task["issue_number"],
                session_id=session_id,
                retry_after=retry_at,
            )
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=0,
                session_id=session_id,
                usage=usage,
                result=result,
                error="TEST_INJECTED_CODEX_SESSION_END",
                status="RETRY",
            )
            return
        try:
            files, _ = validate_changes(self.cfg, workdir, base_sha)
            self.commit_and_push(workdir, task, branch)
            new_sha = git_worktree(workdir, "rev-parse", "HEAD", check=True).stdout.strip()
            if task["task_type"] == "BUILD":
                pr = self.create_pr(issue, task, branch, new_sha, files)
                pr_number = int(pr["number"])
            else:
                pr_number = int(task["pr_number"])
            self.ledger.update(
                task["id"],
                status="DONE",
                target_sha=new_sha,
                pr_number=pr_number,
                session_id=session_id,
                retry_at=None,
                last_error=None,
            )
            self.enqueue_review(task, pr_number, new_sha)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=0,
                session_id=session_id,
                usage=usage,
                result=result,
                status="DONE",
            )
        except Exception as exc:
            self.block(self.ledger.get(task["id"]), str(exc))
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=1,
                session_id=session_id,
                usage=usage,
                result=result,
                error=str(exc),
                status="BLOCKED",
            )

    def invoke_codex(
        self, task: sqlite3.Row, workdir: Path, prompt: str, unit: str
    ) -> subprocess.CompletedProcess[str]:
        command: list[str]
        if task["session_id"]:
            command = [
                "/usr/local/bin/codex",
                "exec",
                "resume",
                str(task["session_id"]),
                "--json",
                "--sandbox",
                "workspace-write",
                "-",
            ]
        else:
            command = [
                "/usr/local/bin/codex",
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "-",
            ]
        full = model_sandbox_command(
            unit=unit, user=CODEX_USER, home=CODEX_HOME, workdir=workdir, command=command
        )
        return run(full, input_text=prompt, timeout=int(self.cfg["build_timeout_seconds"]))

    def codex_prompt(
        self,
        issue: dict[str, Any],
        task: sqlite3.Row,
        comments: list[str],
        blockers: list[str],
    ) -> str:
        comment_text = "\n\n---\n\n".join(comments) if comments else "(none)"
        repair = ""
        if task["task_type"] == "REPAIR":
            repair = (
                "\nThis is a repair pass. Fix ONLY the review blockers below "
                "and necessary adjacent tests. "
                "Do not re-audit the repository.\nBLOCKERS:\n"
                + "\n".join(f"- {x}" for x in blockers)
            )
        return f"""You are the CODEX_CHATGPT engineering builder for the Hyperliquid project.
One scoped task only: GitHub Issue #{issue["number"]}: {issue.get("title", "")}

Hard boundaries:
- Repository is exactly {REPO}.
- REAL TRADING REMAINS DISABLED. Never enable live/real trading, add/use trading keys,
  place orders, change capital/risk authorization, or bypass LIVE_TRADING_GATE.
- Do not access or discuss Polymarket. The sandbox hides /root and /mnt.
- You have no GitHub credentials. Do not try to push, merge, comment, or use GitHub auth.
- Edit only this task checkout. Do not commit; the orchestrator handles Git/GitHub writes.
- Never weaken tests/policy to make a task pass.

Context discipline:
1. Read AGENTS.md.
2. Read docs/ai-team/CURRENT_STATE.md.
3. Use the Issue text below as the success criteria.
4. Use docs/ai-team/SYSTEM_MAP.md only to locate the relevant subsystem.
5. Read only linked/relevant files. No recursive whole-repository audit.
6. Run the narrow relevant tests/lint you can run in this checkout.
{repair}

ISSUE BODY:
{issue.get("body") or ""}

LATEST TRUSTED NON-MACHINE COMMENTS:
{comment_text}

Finish by stating what changed, tests run, and any blocker. Keep changes scoped.
"""

    def commit_and_push(self, workdir: Path, task: sqlite3.Row, branch: str) -> None:
        uid = int(run(["id", "-u", CODEX_USER], check=True).stdout.strip())
        gid = int(run(["id", "-g", CODEX_USER], check=True).stdout.strip())
        prefix = [
            "setpriv",
            f"--reuid={uid}",
            f"--regid={gid}",
            "--init-groups",
            "git",
            "-C",
            str(workdir),
        ]
        run([*prefix, "add", "-A"], check=True)
        message = (
            f"Issue #{task['issue_number']}: autonomous {str(task['task_type']).lower()} via Codex"
        )
        cp = run([*prefix, "commit", "-m", message], timeout=60)
        if cp.returncode != 0:
            raise RuntimeError(f"commit failed: {cp.stderr[-1000:] or cp.stdout[-1000:]}")
        cp = run(
            [
                "git",
                "-c",
                f"safe.directory={workdir}",
                "-C",
                str(workdir),
                "push",
                GIT_PUSH_REMOTE,
                f"HEAD:refs/heads/{branch}",
            ],
            timeout=120,
        )
        if cp.returncode != 0:
            raise RuntimeError(f"push failed: {cp.stderr[-1200:]}")

    def create_pr(
        self,
        issue: dict[str, Any],
        task: sqlite3.Row,
        branch: str,
        sha: str,
        files: list[str],
    ) -> dict[str, Any]:
        body = f"""## Objective
Autonomous implementation for Issue #{issue["number"]}: {issue.get("title", "")}

## GitHub Issue
Closes #{issue["number"]}

## Lane / subsystem
AI-team infrastructure / assigned Issue scope

## Builder / independent reviewer
Builder (logical agent): CODEX_CHATGPT
Reviewer (logical agent): CLAUDE
Reviewed commit SHA: pending independent review

## Before
Issue acceptance criteria not yet implemented.

## After
Codex produced a scoped implementation. Changed files: {", ".join(files[:20])}

## Profitability impact
No profitability claim from this PR unless the Issue explicitly provides
independently reviewed evidence.

## Tests / validation
See Codex task evidence and CI.

## Production impact / rollback
Routine changes remain gated by exact-SHA Claude review and CI before merge.

## Durable-state impact
- [x] no durable-state change required unless the scoped Issue requires it

## Live trading
- [x] No real-trading permission, key, order-route or safety-threshold change
- [x] REAL_TRADING_ENABLED remains disabled

LIVE-SENSITIVE: NO

<!-- AI_TEAM_BUILDER_EVIDENCE
BUILDER=CODEX_CHATGPT
BUILT_SHA={sha}
ASSIGNMENT_ID={task["id"]}
TASK_CLASS={task["task_class"]}
-->
"""
        return self.gh.create_pr(
            title=f"AI team: #{issue['number']} {issue.get('title', '')}"[:240],
            head=branch,
            base="main",
            body=body,
        )

    def enqueue_review(self, parent: sqlite3.Row, pr_number: int, sha: str) -> None:
        issue = self.gh.issue(int(parent["issue_number"]))
        _, escalation_reason = parse_task_class(str(issue.get("body") or ""))
        model = route_review(self.cfg, str(parent["task_class"]), escalation_reason)
        task_id = self.ledger.create_task(
            issue_number=int(parent["issue_number"]),
            pr_number=pr_number,
            task_type="REVIEW",
            agent="CLAUDE",
            model_class=model,
            task_class=str(parent["task_class"]),
            target_sha=sha,
            previous_sha=parent["previous_sha"],
            blockers=json.loads(parent["blockers_json"] or "[]"),
            parent_id=str(parent["id"]),
        )
        self.gh.comment(
            pr_number,
            assignment_marker(
                task_id=task_id,
                agent="CLAUDE",
                task_type="REVIEW",
                model_class=model,
                task_class=str(parent["task_class"]),
                issue_number=int(parent["issue_number"]),
                pr_number=pr_number,
                target_sha=sha,
                parent_id=str(parent["id"]),
                previous_sha=parent["previous_sha"],
                escalation_reason=escalation_reason,
            ),
        )
        self.gh.add_labels(pr_number, [self.cfg["labels"]["waiting_review"]])

    def handle_review(self, task: sqlite3.Row) -> None:
        if not task["pr_number"] or not task["target_sha"]:
            self.block(task, "review missing PR/SHA")
            return
        pr = self.gh.pr(int(task["pr_number"]))
        current_sha = str(pr["head"]["sha"])
        target_sha = str(task["target_sha"])
        if current_sha != target_sha:
            self.ledger.update(task["id"], status="STALE", last_error=f"PR moved to {current_sha}")
            self.enqueue_replacement_review(task, current_sha)
            return
        model = str(task["model_class"])
        issue = self.gh.issue(int(task["issue_number"]))
        if model == "OPUS":
            _, reason = parse_task_class(str(issue.get("body") or ""))
            route_review(self.cfg, str(task["task_class"]), reason)
        try:
            claude_runtime_preflight()
        except RuntimeError as exc:
            self.block(task, f"CLAUDE_AUTH_REQUIRED: {exc}")
            return
        workdir = (
            Path(task["workdir"])
            if task["workdir"]
            else prepare_checkout(
                user=CLAUDE_USER,
                home=CLAUDE_HOME,
                base_dir=CLAUDE_WORK,
                task_id=str(task["id"]),
                ref=target_sha,
                branch=None,
            )
        )
        self.ledger.update(
            task["id"], status="RUNNING", workdir=str(workdir), attempt=int(task["attempt"]) + 1
        )
        task = self.ledger.get(task["id"])
        changed = self.gh.changed_files(int(task["pr_number"]))
        pr_comments = recent_human_comments(self.gh, int(task["pr_number"]), self.trusted)
        blockers = json.loads(task["blockers_json"] or "[]")
        prompt = self.review_prompt(pr, task, changed, pr_comments, blockers)
        if (
            acceptance_flag(str(issue.get("body") or ""), "AI_TEAM_TEST_FORCE_FIRST_REVIEW_FAIL")
            and not task["previous_sha"]
        ):
            prompt += """

ACCEPTANCE FAULT INJECTION — documentation-only orchestration test:
This is the first review of an explicitly flagged acceptance-test Issue. Even if the
implementation is otherwise correct, return VERDICT=FAIL with exactly one blocker:
`Change the documentation-only acceptance marker REPAIR_STAGE=INITIAL to
REPAIR_STAGE=REPAIRED.` Do not invent any other blocker. This fault injection must
not run on re-review because previous_sha is then populated.
"""
        log_path = CLAUDE_LOG / f"{task['id']}-attempt-{task['attempt']}.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        run_id = self.ledger.open_run(task, log_path)
        unit = f"hl-ai-claude-{task['id'][:10]}-{int(time.time())}"
        self.runtime.run_started(run_id, task, prompt=prompt, systemd_unit=unit)
        self.sync_runtime_checkpoint()
        try:
            cp = self.invoke_claude(task, workdir, prompt, unit)
        except subprocess.TimeoutExpired as exc:
            text = (exc.stdout or "") + "\n" + (exc.stderr or "")
            session_id, usage, result = parse_claude_output(text)
            log_path.write_text(bounded_redacted(text))
            self.ledger.close_run(
                run_id,
                exit_code=124,
                session_id=session_id,
                usage=usage,
                result=result,
                error="Claude timeout",
            )
            self.retry_or_block(task, "Claude timeout", session_id=session_id)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                exit_code=124,
                session_id=session_id,
                usage=usage,
                result=result,
                error="Claude timeout",
            )
            return
        combined = cp.stdout + ("\n" + cp.stderr if cp.stderr else "")
        log_path.write_text(bounded_redacted(combined))
        session_id, usage, result = parse_claude_output(cp.stdout)
        limited, retry_at = rate_limit_info(
            combined, int(self.cfg["default_rate_limit_retry_seconds"])
        )
        self.ledger.close_run(
            run_id,
            exit_code=cp.returncode,
            session_id=session_id,
            usage=usage,
            result=result,
            error=None if cp.returncode == 0 else combined[-1500:],
        )
        if cp.returncode != 0:
            if limited:
                self.ledger.update(
                    task["id"],
                    status="WAITING_RATE_LIMIT",
                    retry_at=retry_at,
                    session_id=session_id,
                    last_error="Claude rate/usage limit",
                )
                self.finish_runtime_run(
                    run_id,
                    str(task["id"]),
                    stdout=cp.stdout,
                    stderr=cp.stderr,
                    exit_code=cp.returncode,
                    session_id=session_id,
                    usage=usage,
                    result=result,
                    error="Claude rate/usage limit",
                )
                return
            self.retry_or_block(task, f"Claude failed rc={cp.returncode}", session_id=session_id)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=cp.returncode,
                session_id=session_id,
                usage=usage,
                result=result,
            )
            return
        if (
            acceptance_flag(str(issue.get("body") or ""), "AI_TEAM_TEST_CLAUDE_RATE_LIMIT_ONCE")
            and int(task["attempt"]) == 1
        ):
            retry_at = retry_at_after(300)
            self.ledger.update(
                task["id"],
                status="WAITING_RATE_LIMIT",
                retry_at=retry_at,
                session_id=session_id,
                last_error="TEST_INJECTED_CLAUDE_RATE_LIMIT",
            )
            self.runtime.event(
                "TEST_CLAUDE_RATE_LIMIT_INJECTED",
                assignment_id=task["id"],
                issue=task["issue_number"],
                pr=task["pr_number"],
                target_sha=target_sha,
                session_id=session_id,
                retry_after=retry_at,
            )
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=75,
                session_id=session_id,
                usage=usage,
                result=result,
                error="TEST_INJECTED_CLAUDE_RATE_LIMIT",
                status="WAITING_RATE_LIMIT",
            )
            return
        try:
            verdict, blockers, summary = extract_review(result, target_sha)
        except Exception as exc:
            self.retry_or_block(task, str(exc), session_id=session_id)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=1,
                session_id=session_id,
                usage=usage,
                result=result,
                error=str(exc),
            )
            return
        after = self.gh.pr(int(task["pr_number"]))
        if str(after["head"]["sha"]) != target_sha:
            self.ledger.update(task["id"], status="STALE", last_error="PR changed during review")
            self.enqueue_replacement_review(task, str(after["head"]["sha"]))
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=1,
                session_id=session_id,
                usage=usage,
                result=result,
                error="PR changed during review",
                status="STALE",
            )
            return
        self.gh.comment(
            int(task["pr_number"]),
            result_marker(
                task_id=str(task["id"]),
                reviewed_sha=target_sha,
                verdict=verdict,
                reviewer="CLAUDE",
                model_class=str(task["model_class"]),
                blockers=blockers,
                summary=summary,
            )
            + "\n\n"
            + result[:7000],
        )
        if verdict == "FAIL":
            self.ledger.update(
                task["id"],
                status="DONE",
                blockers_json=json.dumps(blockers),
                session_id=session_id,
                last_error="review FAIL",
            )
            self.enqueue_repair(task, blockers)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=0,
                session_id=session_id,
                usage=usage,
                result=result,
                status="FAIL",
                blockers=blockers,
            )
        else:
            self.ledger.update(
                task["id"],
                status="WAITING_CI",
                blockers_json="[]",
                session_id=session_id,
                retry_at=utcnow(),
                last_error=None,
            )
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=0,
                session_id=session_id,
                usage=usage,
                result=result,
                status="PASS",
                blockers=[],
            )

    def invoke_claude(
        self, task: sqlite3.Row, workdir: Path, prompt: str, unit: str
    ) -> subprocess.CompletedProcess[str]:
        model = "opus" if task["model_class"] == "OPUS" else "sonnet"
        if task["session_id"]:
            command = [
                "/usr/bin/claude",
                "-p",
                "--resume",
                str(task["session_id"]),
                "--model",
                model,
                "--output-format",
                "json",
                "--permission-mode",
                "dontAsk",
            ]
        else:
            command = [
                "/usr/bin/claude",
                "-p",
                "--model",
                model,
                "--output-format",
                "json",
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                "Read,Glob,Grep,Bash",
            ]
        full = model_sandbox_command(
            unit=unit,
            user=CLAUDE_USER,
            home=CLAUDE_HOME,
            workdir=workdir,
            command=command,
        )
        return run(full, input_text=prompt, timeout=int(self.cfg["review_timeout_seconds"]))

    def review_prompt(
        self,
        pr: dict[str, Any],
        task: sqlite3.Row,
        changed: list[str],
        comments: list[str],
        blockers: list[str],
    ) -> str:
        target = str(task["target_sha"])
        delta = ""
        if task["previous_sha"]:
            delta = (
                f"\nThis is a re-review. Previous reviewed SHA: {task['previous_sha']}. "
                f"Review the prior blockers plus ONLY the delta "
                f"`git diff {task['previous_sha']}..{target} -- <changed files>` and "
                "necessary adjacent context. Do not restart a repository audit.\n"
            )
        return f"""You are CLAUDE, the independent adversarial reviewer for the Hyperliquid project.
Review exactly PR #{task["pr_number"]} at exact SHA {target}.
Model routing for this assignment is fixed by the orchestrator: {task["model_class"]}.

Hard boundaries:
- REAL TRADING REMAINS DISABLED. Flag any attempt to enable it, add/use keys, place orders,
  change capital/risk authorization, or bypass LIVE_TRADING_GATE.
- Do not access or discuss Polymarket. The sandbox hides /root and /mnt.
- You have no GitHub credentials and must not push/merge/comment.
- Review independently; try to falsify the implementation and claimed evidence.

Context discipline:
1. Read AGENTS.md and docs/ai-team/CURRENT_STATE.md.
2. Read the PR body below and only the changed files listed below.
3. Use linked policy/subsystem docs only if needed.
4. No recursive whole-repository audit.
{delta}
PR TITLE:
{pr.get("title", "")}

PR BODY:
{pr.get("body") or ""}

CHANGED FILES:
{chr(10).join("- " + x for x in changed)}

PREVIOUS BLOCKERS (if any):
{chr(10).join("- " + x for x in blockers) if blockers else "(none)"}

LATEST TRUSTED PR COMMENTS:
{chr(10).join(comments[-6:]) if comments else "(none)"}

Run narrow relevant tests if useful. At the very end emit EXACTLY these machine-readable lines:
REVIEWED_SHA={target}
VERDICT=PASS
BLOCKERS_JSON=[]

If any merge-blocking defect exists, instead emit:
REVIEWED_SHA={target}
VERDICT=FAIL
BLOCKERS_JSON=["concise blocker 1","concise blocker 2"]

The reviewed SHA must be exactly the target SHA.
"""

    def enqueue_repair(self, review: sqlite3.Row, blockers: list[str]) -> None:
        task_id = self.ledger.create_task(
            issue_number=int(review["issue_number"]),
            pr_number=int(review["pr_number"]),
            task_type="REPAIR",
            agent="CODEX_CHATGPT",
            model_class="CODEX_DEFAULT",
            task_class=str(review["task_class"]),
            branch=None,
            previous_sha=str(review["target_sha"]),
            blockers=blockers,
            parent_id=str(review["id"]),
        )
        self.gh.comment(
            int(review["pr_number"]),
            assignment_marker(
                task_id=task_id,
                agent="CODEX_CHATGPT",
                task_type="REPAIR",
                model_class="CODEX_DEFAULT",
                task_class=str(review["task_class"]),
                issue_number=int(review["issue_number"]),
                pr_number=int(review["pr_number"]),
                target_sha=str(review["target_sha"]),
                parent_id=str(review["id"]),
                previous_sha=str(review["target_sha"]),
            ),
        )

    def enqueue_replacement_review(self, old: sqlite3.Row, current_sha: str) -> None:
        task_id = self.ledger.create_task(
            issue_number=int(old["issue_number"]),
            pr_number=int(old["pr_number"]),
            task_type="REVIEW",
            agent="CLAUDE",
            model_class=str(old["model_class"]),
            task_class=str(old["task_class"]),
            target_sha=current_sha,
            previous_sha=str(old["target_sha"]),
            blockers=json.loads(old["blockers_json"] or "[]"),
            parent_id=str(old["id"]),
        )
        self.gh.comment(
            int(old["pr_number"]),
            assignment_marker(
                task_id=task_id,
                agent="CLAUDE",
                task_type="REVIEW",
                model_class=str(old["model_class"]),
                task_class=str(old["task_class"]),
                issue_number=int(old["issue_number"]),
                pr_number=int(old["pr_number"]),
                target_sha=current_sha,
                parent_id=str(old["id"]),
                previous_sha=str(old["target_sha"]),
            ),
        )

    def handle_ci(self, task: sqlite3.Row) -> None:
        if not task["pr_number"] or not task["target_sha"]:
            self.block(task, "CI wait missing PR/SHA")
            return
        pr = self.gh.pr(int(task["pr_number"]))
        target = str(task["target_sha"])
        if str(pr["head"]["sha"]) != target:
            self.ledger.update(task["id"], status="STALE", last_error="PR moved after PASS")
            self.enqueue_replacement_review(task, str(pr["head"]["sha"]))
            return
        state, detail = self.gh.check_state(target)
        if state == "PENDING":
            retry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                seconds=int(self.cfg["poll_seconds"])
            )
            self.ledger.update(
                task["id"],
                status="WAITING_CI",
                retry_at=retry.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                last_error=f"CI pending: {detail}",
            )
            return
        if state == "FAIL":
            self.block(task, f"CI failed after review PASS: {detail}")
            return
        files = self.gh.changed_files(int(task["pr_number"]))
        sensitive = any(
            name.startswith(prefix)
            for name in files
            for prefix in self.cfg["safety"]["no_auto_merge_path_prefixes"]
        )
        if sensitive:
            self.block(task, "owner-sensitive/live path cannot auto-merge")
            return
        if str(task["task_class"]) not in self.cfg["auto_merge_task_classes"]:
            self.ledger.update(
                task["id"],
                status="DONE",
                retry_at=None,
                last_error="PASS+CI green; task class requires non-automatic merge",
            )
            self.gh.comment(
                int(task["pr_number"]),
                "AI team gate: exact-SHA review PASS and CI green. "
                "Auto-merge withheld by model-routing/risk policy.",
            )
            return
        merged = self.gh.merge(int(task["pr_number"]), target)
        if not merged or not merged.get("merged"):
            self.block(task, f"merge rejected: {merged}")
            return
        self.ledger.update(task["id"], status="DONE", retry_at=None, last_error=None)
        self.gh.remove_label(int(task["pr_number"]), self.cfg["labels"]["waiting_review"])
        self.gh.add_labels(int(task["issue_number"]), [self.cfg["labels"]["done"]])
        self.gh.remove_label(int(task["issue_number"]), self.cfg["labels"]["pending"])
        self.gh.comment(
            int(task["issue_number"]),
            f"AI_TEAM_AUTONOMOUS_MERGE=YES\nPR={task['pr_number']}\nREVIEWED_SHA={target}\n"
            f"CI=PASS\nMERGED_AT={utcnow()}",
        )

    def retry_or_block(
        self,
        task: sqlite3.Row,
        error: str,
        *,
        session_id: str | None = None,
        rate_limited: bool = False,
    ) -> None:
        attempts = int(task["attempt"])
        if attempts >= int(self.cfg["max_attempts"]):
            self.block(task, f"{error}; max attempts reached")
            return
        retry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60 * max(1, attempts))
        self.ledger.update(
            task["id"],
            status="WAITING_RATE_LIMIT" if rate_limited else "RETRY",
            retry_at=retry.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            session_id=session_id or task["session_id"],
            last_error=error,
        )

    def block(self, task: sqlite3.Row, error: str) -> None:
        self.ledger.update(task["id"], status="BLOCKED", retry_at=None, last_error=error[:1500])
        number = int(task["pr_number"] or task["issue_number"])
        try:
            self.gh.add_labels(number, [self.cfg["labels"]["blocked"]])
            self.gh.comment(
                number,
                f"<!-- AI_TEAM_BLOCKED_V1\nASSIGNMENT_ID={task['id']}\n"
                f"ERROR={error[:1000].replace(chr(10), ' ')}\n-->\n"
                f"Autonomous task blocked: {error[:1200]}",
            )
        except Exception:
            pass
        try:
            self.runtime.event(
                "TASK_BLOCKED",
                assignment_id=task["id"],
                issue=task["issue_number"],
                pr=task["pr_number"],
                agent=task["agent"],
                error=error,
            )
            self.sync_runtime_checkpoint()
        except Exception:
            pass


def print_status(ledger: Ledger) -> None:
    snap = ledger.status_snapshot()
    cur = snap["current"]
    print("hyperliquid_ai_team_status=OK")
    print("repository=" + REPO)
    print("polymarket_scope=DENIED")
    print("real_trading=DISABLED_BY_POLICY")
    if not cur:
        print("current_task=IDLE")
        print("codex_status=IDLE")
        print("claude_status=IDLE")
        print("selected_model=NONE")
        print("current_pr=NONE")
        print("current_sha=NONE")
        print("rate_limit_state=NONE")
    else:
        print(f"current_task={cur['id']} {cur['task_type']} issue={cur['issue_number']}")
        print("codex_status=" + (cur["status"] if cur["agent"] == "CODEX_CHATGPT" else "IDLE"))
        print("claude_status=" + (cur["status"] if cur["agent"] == "CLAUDE" else "IDLE"))
        print(f"selected_model={cur['model_class']}")
        print(f"current_pr={cur['pr_number'] or 'NONE'}")
        print(f"current_sha={cur['target_sha'] or 'NONE'}")
        print(f"pending_blocker={cur['last_error'] or 'NONE'}")
        if cur["status"] == "WAITING_RATE_LIMIT":
            print(f"rate_limit_state=WAITING retry_at={cur['retry_at']}")
        else:
            print("rate_limit_state=NONE")
    rev = snap["last_review"]
    if rev:
        verdict = "FAIL" if rev.get("last_error") == "review FAIL" else "PASS_OR_COMPLETE"
        print(f"last_verdict={verdict} sha={rev.get('target_sha') or 'NONE'}")
    else:
        print("last_verdict=NONE")
    print(f"last_successful_run={snap['last_success'] or 'NONE'}")
    print("recent_failures=" + json.dumps(snap["failures"], separators=(",", ":")))
    runtime = RuntimeLedgerFiles(STATE_ROOT, DB_PATH, REPO, RUNTIME_STATUS_ISSUE)
    projection = runtime.project_current()
    print(f"runtime_status_issue={RUNTIME_STATUS_ISSUE}")
    for agent_key in ("codex", "claude"):
        assignment = projection.get("assignment", {}).get(agent_key)
        if assignment:
            print(f"{agent_key}_current_step={assignment.get('current_step') or 'NONE'}")
            print(f"{agent_key}_next_step={assignment.get('next_step') or 'NONE'}")
            print(f"{agent_key}_checkpoint_retry={assignment.get('retry_after') or 'NONE'}")
        else:
            print(f"{agent_key}_current_step=IDLE")
            print(f"{agent_key}_next_step=NONE")
            print(f"{agent_key}_checkpoint_retry=NONE")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--init-db", action="store_true")
    args = parser.parse_args()
    cfg = load_config()
    if cfg.get("repository") != REPO:
        raise RuntimeError("repository safety mismatch")
    for p in (DB_PATH.parent, CODEX_WORK, CLAUDE_WORK, CODEX_LOG, CLAUDE_LOG, LOCK_PATH.parent):
        p.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(DB_PATH)
    if args.status:
        print_status(ledger)
        return 0
    if args.init_db and not args.once:
        return 0
    lockf = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    orch = Orchestrator()
    try:
        orch.cycle()
        return 0
    except Exception as exc:
        print(f"orchestrator_error={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
