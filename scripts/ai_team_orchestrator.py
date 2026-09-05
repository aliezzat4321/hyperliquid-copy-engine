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
import hashlib
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
TRELLO_BRIDGE = Path("/opt/hyperliquid-ai-team/scripts/trello_team_bridge.py")

COMPLETION_REQUIREMENTS = {
    "RUNTIME_PROOF": ("PRODUCTION_VALIDATION",),
    "DEPLOYMENT_RUNTIME_PROOF": ("DEPLOY", "PRODUCTION_VALIDATION"),
    "MEASUREMENT_PROOF": ("MEASUREMENT",),
    "PROSPECTIVE_EVIDENCE": (
        "POST_MERGE_EVIDENCE", "EVIDENCE_AUDIT", "FINAL_VERDICT"
    ),
    "STORAGE_PROOF": (
        "PRODUCTION_AUDIT", "IMMUTABLE_PLAN", "DESTRUCTIVE_REVIEW",
        "AUTHORIZED_APPLY", "MEASUREMENT", "STABILITY_VALIDATION",
    ),
}
EVIDENCE_TASK_TYPES = {phase for phases in COMPLETION_REQUIREMENTS.values() for phase in phases}

ACCEPTANCE_ARTIFACT_SCHEMA_VERSION = "ai-team-acceptance-v1"
ACCEPTANCE_POLICY_VERSION = "2026-09-05"
PHASE_EVIDENCE_SPECS: dict[str, dict[str, Any]] = {
    "DEPLOY": {"producers": {"deployment-controller"}, "field": "status", "value": "DEPLOYED"},
    "PRODUCTION_VALIDATION": {
        "producers": {"production-validation-runner"}, "field": "status", "value": "PASS"
    },
    "MEASUREMENT": {
        "producers": {"measurement-runner"}, "field": "verdict", "value": "PASS",
        "window": True,
    },
    "POST_MERGE_EVIDENCE": {
        "producers": {"post-merge-evidence-runner"}, "field": "status",
        "value": "COMPLETE", "window": True,
    },
    "EVIDENCE_AUDIT": {
        "producers": {"evidence-audit-runner"}, "field": "verdict", "value": "PASS",
        "window": True,
    },
    "FINAL_VERDICT": {
        "producers": {"final-verdict-runner"}, "field": "verdict", "value": "PASS",
        "window": True,
    },
    "PRODUCTION_AUDIT": {
        "producers": {"production-audit-runner"}, "field": "verdict", "value": "PASS"
    },
    "IMMUTABLE_PLAN": {
        "producers": {"immutable-plan-runner"}, "field": "status", "value": "FROZEN"
    },
    "DESTRUCTIVE_REVIEW": {
        "producers": {"destructive-review-runner"}, "field": "verdict", "value": "PASS"
    },
    "AUTHORIZED_APPLY": {
        "producers": {"authorized-apply-runner"}, "field": "status", "value": "APPLIED"
    },
    "STABILITY_VALIDATION": {
        "producers": {"stability-validation-runner"}, "field": "verdict", "value": "PASS",
        "window": True,
    },
}

# Protected AI-control-plane files that Codex may propose, but never merge merely
# because it changed them. Automatic apply additionally requires a trusted Issue
# flag, independent exact-SHA Claude PASS, and green CI.
# Trading/live/capital/deployment paths are intentionally excluded.
AUTO_APPLY_CONTROL_PLANE_PATHS = {
    "config/ai_team_router.json",
    "scripts/ai_team_orchestrator.py",
    "scripts/ai_team_runtime_ledger.py",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "protocol_version": 1,
    "repository": REPO,
    "routing": {
        "BUILD": {"agent": "CODEX_CHATGPT", "model_class": "CODEX_DEFAULT"},
        "REPAIR": {"agent": "CODEX_CHATGPT", "model_class": "CODEX_DEFAULT"},
        "REVIEW": {"agent": "CLAUDE", "model_class": "SONNET"},
        "RESEARCH": {"agent": "CLAUDE", "model_class": "OPUS"},
    },
    "initial_routes": {
        "ROUTINE": {"task_type": "BUILD", "agent": "CODEX_CHATGPT", "model_class": "CODEX_DEFAULT"},
        **{name: {"task_type": "RESEARCH", "agent": "CLAUDE", "model_class": "OPUS"}
           for name in ("QUANT_PROFITABILITY", "STATISTICAL_METHODOLOGY", "MAJOR_ARCHITECTURE",
                        "UNRESOLVED_DISAGREEMENT", "CAPITAL_SENSITIVE_METHODOLOGY")},
    },
    "remediation": {
        "classes": ["CODE_CHANGE", "PR_METADATA", "PROTECTED_ACTION", "CI_RETRY",
                    "REVIEW_RERUN", "POLICY_RECONCILIATION", "TERMINAL"],
        "actors": {"CODE_CHANGE": "CODEX_CHATGPT", "PR_METADATA": "MANAGER",
                   "PROTECTED_ACTION": "TRUSTED_MANAGER", "CI_RETRY": "MANAGER",
                   "REVIEW_RERUN": "MANAGER", "POLICY_RECONCILIATION": "MANAGER",
                   "TERMINAL": "MANAGER"},
        "budgets": {"CODE_CHANGE": 1, "PR_METADATA": 3, "PROTECTED_ACTION": 1,
                    "CI_RETRY": 3, "REVIEW_RERUN": 0, "POLICY_RECONCILIATION": 3,
                    "TERMINAL": 0},
        "protected_actions": {},
    },
    "legacy_remediation_migration": {"version": 1, "issues": [166, 168, 170],
                                     "supersede_issue": 170, "release_issue": 120},
    "completion_reconciliation": {
        "120": ["STORAGE_PROOF"],
        "93": ["RUNTIME_PROOF"], "196": ["PROSPECTIVE_EVIDENCE"],
        "197": ["PROSPECTIVE_EVIDENCE"], "91": ["PROSPECTIVE_EVIDENCE"],
        "92": ["MEASUREMENT_PROOF"], "150": ["MEASUREMENT_PROOF"],
    },
    "opus_allowed_task_classes": [
        "QUANT_PROFITABILITY",
        "STATISTICAL_METHODOLOGY",
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
    "auto_merge_task_classes": [
        "ROUTINE",
        "QUANT_PROFITABILITY",
        "STATISTICAL_METHODOLOGY",
        "MAJOR_ARCHITECTURE",
        "UNRESOLVED_DISAGREEMENT",
        "CAPITAL_SENSITIVE_METHODOLOGY",
    ],
    "max_attempts": 3,
    "poll_seconds": 60,
    "default_rate_limit_retry_seconds": 3600,
    "claude_readiness_probe_seconds": 300,
    "claude_readiness_probe_timeout_seconds": 20,
    "claude_readiness_probe_output_bytes": 4096,
    "claude_turn_budgets": {"REVIEW": 12, "RESEARCH": 16},
    "review_timeout_seconds": 1200,
    "research_timeout_seconds": 1800,
    "build_timeout_seconds": 1800,
    "trusted_author_associations": ["OWNER", "MEMBER", "COLLABORATOR"],
    "labels": {
        "ready": "ai-team:ready",
        "queued": "ai-team:queued",
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
            ".github/workflows/",
            "config/ai_team_router.json",
            "deploy/systemd/",
            "scripts/ai_team_",
            "scripts/install_codex_code_mode_host.sh",
            "scripts/install_ai_team_orchestrator.sh",
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
        normalized = value[:-1] + "+00:00" if value.lower().endswith("z") else value
        parsed = dt.datetime.fromisoformat(normalized)
        return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed
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
    """Run manager-side read-only Git without refreshing the agent-owned index."""
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
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
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

    def pending_issues(self, label: str) -> list[dict[str, Any]]:
        return self.ready_issues(label)

    def finalizer_issues(self, done_label: str) -> list[dict[str, Any]]:
        label = urllib.parse.quote(done_label, safe="")
        rows = self.api(
            "GET", f"repos/{self.repo}/issues?state=all&labels={label}"
            "&sort=updated&direction=desc&per_page=100",
        ) or []
        return [
            row for row in rows
            if "pull_request" not in row and finalizes_parent(str(row.get("body") or ""))
        ]

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

    def failed_check_blockers(self, sha: str) -> list[dict[str, Any]]:
        data = self.api("GET", f"repos/{self.repo}/commits/{sha}/check-runs?per_page=100") or {}
        result = []
        transient = {"cancelled", "timed_out", "stale", "neutral", "action_required"}
        for check in data.get("check_runs", []):
            conclusion = check.get("conclusion")
            if check.get("status") != "completed" or conclusion in {"success", "skipped"}:
                continue
            klass = "CI_RETRY" if conclusion in transient and check.get("id") else "CODE_CHANGE"
            result.append({
                "protocol_version": 1, "class": klass, "source_kind": "CI",
                "source_id": str(check.get("id")), "subject_sha": sha,
                "rule_id": "TRANSIENT_CHECK" if klass == "CI_RETRY" else "DETERMINISTIC_CI_FAILURE",
                "observed": {"name": str(check.get("name") or "").strip().lower(),
                             "conclusion": conclusion, "run_id": check.get("id")},
                "requested_action": ({"check_run_id": check.get("id"),
                                      "check_name": check.get("name")}
                                     if klass == "CI_RETRY" else
                                     {"check_name": check.get("name"),
                                      "reproducer": str(check.get("name") or "CI check")}),
            })
        return result

    def merge(self, pr_number: int, sha: str) -> dict[str, Any]:
        return self.api(
            "PUT",
            f"repos/{self.repo}/pulls/{pr_number}/merge",
            {"merge_method": "squash", "sha": sha},
        )

    def patch_pr(self, number: int, fields: dict[str, Any]) -> dict[str, Any]:
        return self.api("PATCH", f"repos/{self.repo}/pulls/{number}", fields)

    def rerun_check(self, check_run_id: int) -> Any:
        return self.api("POST", f"repos/{self.repo}/check-runs/{check_run_id}/rerequest")

    def dispatch_workflow(self, workflow_id: str, ref: str, inputs: dict[str, Any]) -> Any:
        workflow = urllib.parse.quote(workflow_id, safe="")
        return self.api(
            "POST", f"repos/{self.repo}/actions/workflows/{workflow}/dispatches",
            {"ref": ref, "inputs": inputs},
        )

    def close_issue(self, number: int) -> None:
        self.api("PATCH", f"repos/{self.repo}/issues/{number}", {"state": "closed"})


class Ledger:
    def __init__(self, path: Path, *, trusted_evidence_root: Path | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.trusted_evidence_root = trusted_evidence_root or STATE_ROOT / "acceptance-artifacts"
        self.trusted_evidence_uid = 0
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
              limit_text TEXT,
              systemd_unit TEXT,
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
            CREATE TABLE IF NOT EXISTS remediations (
              remediation_id TEXT PRIMARY KEY, issue_number INTEGER NOT NULL,
              pr_number INTEGER, subject_sha TEXT NOT NULL, class TEXT NOT NULL,
              rule_id TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
              observed_canonical_json TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
              actor TEXT NOT NULL, action_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
              occurrence_count INTEGER NOT NULL DEFAULT 1,
              action_attempts INTEGER NOT NULL DEFAULT 0, last_action_at TEXT,
              completion_evidence TEXT, parent_assignment_id TEXT,
              requested_action_json TEXT NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS acceptance_evidence (
              evidence_id TEXT PRIMARY KEY, issue_number INTEGER NOT NULL,
              requirement TEXT NOT NULL, phase TEXT NOT NULL, source TEXT NOT NULL,
              observed_at TEXT NOT NULL, window_start TEXT, window_end TEXT,
              code_sha TEXT, data_hash TEXT, manifests_json TEXT NOT NULL,
              measured_result_json TEXT NOT NULL, predicate_result INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(issue_number, requirement, source, observed_at, code_sha, data_hash)
            );
            CREATE TABLE IF NOT EXISTS merged_code (
              issue_number INTEGER PRIMARY KEY, code_sha TEXT NOT NULL,
              pr_number INTEGER NOT NULL, observed_at TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        for name in ("limit_text", "systemd_unit", "lifecycle_phase",
                     "completion_contract_json", "evidence_json"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT")
        evidence_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(acceptance_evidence)")
        }
        if "machine_verified" not in evidence_columns:
            self.db.execute(
                "ALTER TABLE acceptance_evidence ADD COLUMN machine_verified "
                "INTEGER NOT NULL DEFAULT 0"
            )
        self.db.commit()

    def observe_remediation(self, blocker: dict[str, Any], *, issue_number: int,
                            pr_number: int | None, actor: str,
                            parent_assignment_id: str | None = None) -> sqlite3.Row:
        observed = canonical_json(blocker["observed"])
        identity = "\0".join((str(blocker.get("protocol_version", 1)), blocker["class"],
                              blocker["rule_id"], blocker["source_kind"],
                              str(blocker["source_id"]), blocker["subject_sha"], observed))
        fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        action_json = canonical_json(blocker.get("requested_action", {}))
        action_key = hashlib.sha256(
            (fingerprint + "\0" + actor + "\0" + action_json).encode()
        ).hexdigest()
        now = utcnow()
        self.db.execute(
            "INSERT INTO remediations(remediation_id,issue_number,pr_number,subject_sha,"
            "class,rule_id,"
            "source_kind,source_id,observed_canonical_json,fingerprint,actor,action_key,status,"
            "parent_assignment_id,requested_action_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET "
            "occurrence_count=occurrence_count+1,updated_at=excluded.updated_at",
            (fingerprint[:24], issue_number, pr_number, blocker["subject_sha"], blocker["class"],
             blocker["rule_id"], blocker["source_kind"], str(blocker["source_id"]), observed,
             fingerprint, actor, action_key, "OBSERVED", parent_assignment_id,
             action_json, now, now),
        )
        self.db.commit()
        return self.db.execute(
            "SELECT * FROM remediations WHERE fingerprint=?", (fingerprint,)
        ).fetchone()

    def update_remediation(self, remediation_id: str, **kw: Any) -> None:
        kw["updated_at"] = utcnow()
        self.db.execute("UPDATE remediations SET " + ",".join(f"{k}=?" for k in kw) +
                        " WHERE remediation_id=?", [*kw.values(), remediation_id])
        self.db.commit()

    def recover_interrupted(self) -> list[dict[str, Any]]:
        now = utcnow()
        rows = [dict(row) for row in self.db.execute("SELECT * FROM tasks WHERE status='RUNNING'")]
        self.db.execute(
            """
            UPDATE tasks
               SET status='RETRY',
                   retry_at=?,
                   systemd_unit=NULL,
                   last_error=COALESCE(last_error,'orchestrator restarted during task'),
                   updated_at=?
             WHERE status='RUNNING'
            """,
            (now, now),
        )
        self.db.commit()
        return rows

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
            "limit_text": kw.get("limit_text"),
            "systemd_unit": kw.get("systemd_unit"),
            "last_error": kw.get("last_error"),
            "parent_id": kw.get("parent_id"),
            "lifecycle_phase": kw.get("lifecycle_phase", "IMPLEMENTING"),
            "completion_contract_json": json.dumps(kw.get("completion_contract") or {}),
            "evidence_json": json.dumps(kw.get("evidence") or {}),
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

    def child(
        self, parent_id: str, task_type: str, target_sha: str | None = None
    ) -> sqlite3.Row | None:
        sql = "SELECT * FROM tasks WHERE parent_id=? AND task_type=?"
        params: list[Any] = [parent_id, task_type]
        if target_sha is not None:
            sql += " AND target_sha=?"
            params.append(target_sha)
        sql += " ORDER BY created_at LIMIT 1"
        return self.db.execute(sql, params).fetchone()

    def phase_task(self, issue_number: int, requirement: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM tasks WHERE issue_number=? AND lifecycle_phase=? "
            "ORDER BY created_at DESC LIMIT 1", (issue_number, requirement)
        ).fetchone()

    def handoff_candidates(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT * FROM tasks
             WHERE (
                    status='DONE'
                AND task_type IN ('BUILD','REPAIR')
                AND pr_number IS NOT NULL
                AND target_sha IS NOT NULL
             ) OR (
                    status='DONE'
                AND task_type='REVIEW'
                AND last_error IN ('review FAIL','CI failure queued for autonomous repair')
             ) OR (
                    status='STALE'
                AND task_type='REVIEW'
                AND (
                       last_error LIKE 'PR moved to %'
                    OR last_error='PR changed during review'
                )
             )
             ORDER BY updated_at
             LIMIT 50
            """
        ).fetchall()

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

    def has_task_for_issue(self, issue_number: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM tasks WHERE issue_number=? LIMIT 1", (issue_number,)
        ).fetchone()
        return bool(row)

    def has_active_work(self) -> bool:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = self.db.execute(
            f"SELECT 1 FROM tasks WHERE status IN ({placeholders}) LIMIT 1",
            tuple(ACTIVE_STATUSES),
        ).fetchone()
        return bool(row)

    def has_queue_claim_conflict(self) -> bool:
        """Keep one active claim, except for a future provider-capacity wait."""
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = self.db.execute(
            f"SELECT 1 FROM tasks WHERE status IN ({placeholders}) "
            "AND NOT (agent='CLAUDE' AND status='WAITING_RATE_LIMIT' "
            "AND retry_at IS NOT NULL AND retry_at > ?) LIMIT 1",
            (*ACTIVE_STATUSES, utcnow()),
        ).fetchone()
        return bool(row)

    def successful_issue(self, issue_number: int) -> bool:
        """Require the latest terminal assignment to be cleanly successful."""
        row = self.db.execute(
            "SELECT status,last_error FROM tasks WHERE issue_number=? "
            "AND status IN ('DONE','FAILED','BLOCKED','STALE') "
            "ORDER BY updated_at DESC, created_at DESC LIMIT 1",
            (issue_number,),
        ).fetchone()
        return bool(row and row["status"] == "DONE" and not row["last_error"])

    def record_merged_code(self, *, issue_number: int, code_sha: str,
                           pr_number: int, observed_at: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", code_sha) or not parse_utc(observed_at):
            raise ValueError("INVALID_MERGED_CODE_EVIDENCE")
        self.db.execute(
            "INSERT INTO merged_code VALUES(?,?,?,?) ON CONFLICT(issue_number) DO UPDATE SET "
            "code_sha=excluded.code_sha,pr_number=excluded.pr_number,"
            "observed_at=excluded.observed_at",
            (issue_number, code_sha, pr_number, observed_at),
        )
        self.db.commit()

    def _verified_acceptance_artifact(self, *, requirement: str, phase: str,
                                      evidence: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Read proof from the manager-owned spool; caller assertions are never proof."""
        if set(evidence) != {"artifact_path", "artifact_hash"}:
            raise ValueError("NON_CANONICAL_ACCEPTANCE_ENVELOPE")
        root = self.trusted_evidence_root.resolve()
        path = Path(str(evidence["artifact_path"]))
        if path.is_symlink() or path.parent.resolve() != root or not path.is_file():
            raise ValueError("UNTRUSTED_ACCEPTANCE_ARTIFACT_PATH")
        root_stat = root.stat()
        stat = path.stat()
        if (root_stat.st_uid != self.trusted_evidence_uid or root_stat.st_mode & 0o022
                or stat.st_uid != self.trusted_evidence_uid or stat.st_mode & 0o022):
            raise ValueError("UNTRUSTED_ACCEPTANCE_ARTIFACT_OWNER")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if (not re.fullmatch(r"[0-9a-f]{64}", str(evidence["artifact_hash"]))
                or digest != evidence["artifact_hash"]):
            raise ValueError("ACCEPTANCE_ARTIFACT_HASH_MISMATCH")
        try:
            artifact = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("INVALID_ACCEPTANCE_ARTIFACT") from exc
        spec = PHASE_EVIDENCE_SPECS.get(phase)
        required = {"schema_version", "policy_version", "requirement", "phase", "producer",
                    "observed_at", "code_sha", "data_hash", "manifests", "result"}
        optional = {"window_start", "window_end", "authorization", "review_model"}
        if (not isinstance(artifact, dict) or set(artifact) - (required | optional)
                or not required.issubset(artifact)):
            raise ValueError("INVALID_ACCEPTANCE_ARTIFACT_SCHEMA")
        if (artifact["schema_version"] != ACCEPTANCE_ARTIFACT_SCHEMA_VERSION
                or artifact["policy_version"] != ACCEPTANCE_POLICY_VERSION):
            raise ValueError("INCOMPATIBLE_ACCEPTANCE_ARTIFACT_VERSION")
        if artifact["requirement"] != requirement or artifact["phase"] != phase or not spec:
            raise ValueError("ACCEPTANCE_ARTIFACT_PHASE_MISMATCH")
        if artifact["producer"] not in spec["producers"]:
            raise ValueError("UNTRUSTED_ACCEPTANCE_PRODUCER")
        observed = parse_utc(str(artifact["observed_at"]))
        now = dt.datetime.now(dt.timezone.utc)
        if (not observed or observed > now + dt.timedelta(minutes=5)
                or now - observed > dt.timedelta(days=7)):
            raise ValueError("STALE_ACCEPTANCE_EVIDENCE")
        if (not re.fullmatch(r"[0-9a-f]{40}", str(artifact["code_sha"]))
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact["data_hash"]))):
            raise ValueError("INVALID_ACCEPTANCE_ARTIFACT_IDENTITY")
        if (not isinstance(artifact["manifests"], dict) or not artifact["manifests"]
                or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value))
                       for value in artifact["manifests"].values())):
            raise ValueError("INVALID_ACCEPTANCE_MANIFEST_HASH")
        if spec.get("window"):
            start = parse_utc(str(artifact.get("window_start")))
            end = parse_utc(str(artifact.get("window_end")))
            if not start or not end or not start < end <= observed:
                raise ValueError("INVALID_ACCEPTANCE_WINDOW")
        if (phase in {"DESTRUCTIVE_REVIEW", "EVIDENCE_AUDIT", "FINAL_VERDICT"}
                and artifact.get("review_model") != "OPUS"):
            raise ValueError("REQUIRED_OPUS_EVIDENCE_MISSING")
        if phase == "AUTHORIZED_APPLY":
            authorization = artifact.get("authorization")
            if (not isinstance(authorization, dict)
                    or not all(authorization.get(key) for key in (
                        "authorized_by", "scope", "issued_at"
                    ))
                    or not parse_utc(str(authorization["issued_at"]))):
                raise ValueError("APPLY_AUTHORIZATION_MISSING")
        if not isinstance(artifact["result"], dict):
            raise ValueError("INVALID_ACCEPTANCE_RESULT")
        return artifact, digest

    def record_acceptance_evidence(self, *, issue_number: int, requirement: str,
                                   phase: str, evidence: dict[str, Any]) -> tuple[str, bool]:
        """Verify immutable machine output and independently recompute its predicate."""
        artifact, digest = self._verified_acceptance_artifact(
            requirement=requirement, phase=phase, evidence=evidence
        )
        merged = self.db.execute(
            "SELECT code_sha FROM merged_code WHERE issue_number=?", (issue_number,)
        ).fetchone()
        if not merged or artifact["code_sha"] != merged["code_sha"]:
            raise ValueError("EVIDENCE_CODE_SHA_MISMATCH")
        spec = PHASE_EVIDENCE_SPECS[phase]
        predicate = artifact["result"].get(spec["field"]) == spec["value"]
        identity = canonical_json({"issue": issue_number, "artifact_hash": digest, **artifact})
        evidence_id = hashlib.sha256(identity.encode()).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO acceptance_evidence("
            "evidence_id,issue_number,requirement,phase,source,observed_at,window_start,"
            "window_end,code_sha,data_hash,manifests_json,measured_result_json,"
            "predicate_result,created_at,machine_verified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (evidence_id, issue_number, requirement, phase, str(artifact["producer"]),
             str(artifact["observed_at"]), artifact.get("window_start"),
             artifact.get("window_end"), artifact["code_sha"], artifact["data_hash"],
             canonical_json(artifact["manifests"]), canonical_json(artifact["result"]),
             int(predicate), utcnow()),
        )
        self.db.commit()
        return evidence_id, predicate

    def proven_requirements(self, issue_number: int) -> set[str]:
        rows = self.db.execute(
            "SELECT e.requirement,e.phase FROM acceptance_evidence e JOIN merged_code m "
            "ON m.issue_number=e.issue_number AND m.code_sha=e.code_sha "
            "WHERE e.issue_number=? AND e.predicate_result=1 AND e.machine_verified=1",
            (issue_number,),
        ).fetchall()
        observed: dict[str, set[str]] = {}
        for requirement, phase in rows:
            observed.setdefault(str(requirement), set()).add(str(phase))
        return {requirement for requirement, phases in COMPLETION_REQUIREMENTS.items()
                if set(phases).issubset(observed.get(requirement, set()))}

    def phase_is_proven(self, issue_number: int, requirement: str, phase: str) -> bool:
        return bool(self.db.execute(
            "SELECT 1 FROM acceptance_evidence e JOIN merged_code m "
            "ON m.issue_number=e.issue_number "
            "AND m.code_sha=e.code_sha WHERE e.issue_number=? AND e.requirement=? "
            "AND e.phase=? AND e.predicate_result=1 AND e.machine_verified=1 LIMIT 1",
            (issue_number, requirement, phase),
        ).fetchone())

    def meta_get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def meta_set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.db.commit()

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


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _machine_values(body: str, name: str) -> list[str]:
    return re.findall(rf"(?mi)^\s*{re.escape(name)}\s*=\s*([^\s]+)\s*$", body or "")


def parse_initial_route(body: str, cfg: dict[str, Any] | None = None) -> dict[str, str]:
    """Strictly parse the initial route. Invalid high-stakes metadata never defaults."""
    cfg = cfg or DEFAULT_CONFIG
    names = ("AI_TASK_CLASS", "AI_INITIAL_ROUTE", "AI_INITIAL_AGENT", "AI_INITIAL_MODEL")
    values = {name: _machine_values(body, name) for name in names}
    if any(len(items) > 1 for items in values.values()):
        raise ValueError("INVALID_INITIAL_ROUTE: duplicate routing field")
    task_class = values["AI_TASK_CLASS"][0] if values["AI_TASK_CLASS"] else None
    # Backward-compatible spelling is accepted only for explicitly routine work.
    legacy = _machine_values(body, "TASK_CLASS")
    if len(legacy) > 1 or (legacy and task_class):
        raise ValueError("INVALID_INITIAL_ROUTE: duplicate/contradictory task class")
    if not task_class and len(legacy) == 1 and legacy[0] == "ROUTINE":
        task_class = "ROUTINE"
    legacy_queue = acceptance_flag(body, "AI_TEAM_AUTO_QUEUE")
    if not task_class and legacy_queue:
        task_class = "ROUTINE"
    if not task_class:
        raise ValueError("INVALID_INITIAL_ROUTE: missing AI_TASK_CLASS")
    expected = cfg.get("initial_routes", {}).get(task_class)
    if not expected:
        raise ValueError("INVALID_INITIAL_ROUTE: task class is not allowlisted")
    supplied = {
        "task_type": values["AI_INITIAL_ROUTE"][0] if values["AI_INITIAL_ROUTE"] else None,
        "agent": values["AI_INITIAL_AGENT"][0] if values["AI_INITIAL_AGENT"] else None,
        "model_class": values["AI_INITIAL_MODEL"][0] if values["AI_INITIAL_MODEL"] else None,
    }
    # Existing trusted queue entries predate the explicit route triple. Migrate them
    # through the reviewed class allowlist; new non-queue entries remain strict.
    if not any(supplied.values()) and (task_class == "ROUTINE" or legacy_queue):
        supplied = dict(expected)
    if supplied != expected:
        raise ValueError("INVALID_INITIAL_ROUTE: incomplete or contradictory route")
    return {"task_class": task_class, **supplied}  # type: ignore[arg-type]


def parse_protected_action_authorization(body: str) -> dict[str, Any] | None:
    """Read the user-issued authorization from the trusted repository Issue."""
    names = (
        "AI_PROTECTED_AUTH_ID", "AI_PROTECTED_AUTH_ACTION",
        "AI_PROTECTED_AUTH_SUBJECT_SHA", "AI_PROTECTED_AUTH_EXPIRES_AT",
        "AI_PROTECTED_AUTH_MAX_ACTIONS",
    )
    values = {name: _machine_values(body, name) for name in names}
    if any(len(items) != 1 for items in values.values()):
        return None
    try:
        maximum = int(values["AI_PROTECTED_AUTH_MAX_ACTIONS"][0])
    except ValueError:
        return None
    return {
        "id": values["AI_PROTECTED_AUTH_ID"][0],
        "action": values["AI_PROTECTED_AUTH_ACTION"][0],
        "subject_sha": values["AI_PROTECTED_AUTH_SUBJECT_SHA"][0],
        "expires_at": values["AI_PROTECTED_AUTH_EXPIRES_AT"][0],
        "max_actions": maximum,
    }


def parse_task_class(body: str) -> tuple[str, str | None]:
    """Parse an explicit task class, failing closed when absent or invalid.

    UNCLASSIFIED uses the routine Sonnet review route but is deliberately absent
    from auto_merge_task_classes. Every explicitly declared recognized task class can
    reach automatic merge only after the independent exact-SHA review and CI gates.
    """
    allowed = {
        "ROUTINE",
        "QUANT_PROFITABILITY",
        "STATISTICAL_METHODOLOGY",
        "MAJOR_ARCHITECTURE",
        "UNRESOLVED_DISAGREEMENT",
        "CAPITAL_SENSITIVE_METHODOLOGY",
    }
    m = re.search(r"(?mi)^\s*(?:AI_)?TASK_CLASS\s*=\s*([A-Z_]+)\s*$", body or "")
    task_class = m.group(1) if m and m.group(1) in allowed else "UNCLASSIFIED"
    e = re.search(r"(?mi)^\s*OPUS_ESCALATION_REASON\s*=\s*([A-Z_]+)\s*$", body or "")
    return task_class, e.group(1) if e else None


def normalize_blocker(blocker: Any, *, subject_sha: str, source_kind: str,
                      source_id: str) -> dict[str, Any]:
    """Validate BLOCKER_V1 without inferring a remediation class from prose."""
    terminal = {"protocol_version": 1, "class": "TERMINAL", "source_kind": source_kind,
                "source_id": str(source_id), "subject_sha": subject_sha,
                "rule_id": "UNCLASSIFIED_BLOCKER", "observed": {"invalid": True},
                "requested_action": {}}
    if not isinstance(blocker, dict):
        return terminal
    required = {"class", "source_kind", "source_id", "subject_sha", "rule_id", "observed"}
    if not required.issubset(blocker) or not isinstance(blocker.get("observed"), dict):
        return terminal
    if blocker["class"] not in DEFAULT_CONFIG["remediation"]["classes"]:
        return terminal
    if (str(blocker["subject_sha"]) != subject_sha or blocker["source_kind"] != source_kind
            or str(blocker["source_id"]) != str(source_id)):
        return terminal
    result = dict(blocker)
    result["protocol_version"] = 1
    result["source_id"] = str(result["source_id"])
    result.setdefault("requested_action", {})
    return result


def route_review(cfg: dict[str, Any], task_class: str, escalation_reason: str | None) -> str:
    if task_class in cfg["opus_allowed_task_classes"]:
        if escalation_reason and escalation_reason not in cfg["opus_allowed_reasons"]:
            raise RuntimeError(f"invalid Opus escalation reason: {escalation_reason}")
        return "OPUS"
    if task_class == "MAJOR_ARCHITECTURE":
        if escalation_reason is None:
            return "SONNET"
        if escalation_reason == "MAJOR_ARCHITECTURE":
            return "OPUS"
        raise RuntimeError(f"invalid Opus escalation reason: {escalation_reason}")
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
    blockers: list[Any],
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


def normalize_worktree_ownership(workdir: Path, user: str) -> tuple[int, int]:
    """Return a generated worktree fully to its dedicated non-root agent identity."""
    uid = int(run(["id", "-u", user], check=True).stdout.strip())
    gid = int(run(["id", "-g", user], check=True).stdout.strip())
    os.chown(workdir, uid, gid)
    for root, dirs, files in os.walk(workdir):
        for name in dirs:
            os.chown(Path(root) / name, uid, gid)
        for name in files:
            os.chown(Path(root) / name, uid, gid)
    return uid, gid


def prepare_checkout(
    *, user: str, home: Path, base_dir: Path, task_id: str, ref: str, branch: str | None = None
) -> Path:
    workdir = base_dir / task_id
    if not workdir.exists():
        workdir.parent.mkdir(parents=True, exist_ok=True)
        cp = run(
            ["git", "clone", "--quiet", f"https://github.com/{REPO}.git", str(workdir)],
            timeout=180,
        )
        if cp.returncode != 0:
            raise RuntimeError(f"clone failed: {cp.stderr[-1200:]}")
        run(["git", "-C", str(workdir), "checkout", "--quiet", ref], timeout=60, check=True)
        if branch:
            run(["git", "-C", str(workdir), "checkout", "-B", branch], timeout=60, check=True)
    uid, gid = normalize_worktree_ownership(workdir, user)
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


def parse_completion_contract(body: str) -> dict[str, Any]:
    """Parse only trusted machine metadata; prose never becomes acceptance proof."""
    matches = re.findall(
        r"(?mi)^\s*AI_TEAM_COMPLETION_REQUIRES\s*=\s*([^\n]+?)\s*$", body or ""
    )
    close_values = _machine_values(body or "", "AI_TEAM_CLOSE_ON_MERGE")
    if len(matches) > 1 or len(close_values) > 1:
        raise ValueError("AMBIGUOUS_COMPLETION_CONTRACT")
    close_on_merge = close_values == ["YES"]
    if close_values and close_values[0] not in {"YES", "NO"}:
        raise ValueError("INVALID_CLOSE_ON_MERGE")
    requirements: list[str] = []
    if matches:
        requirements = [item.strip().upper() for item in matches[0].split(",")]
        if (not all(requirements) or len(set(requirements)) != len(requirements)
                or any(item not in COMPLETION_REQUIREMENTS for item in requirements)):
            raise ValueError("INVALID_COMPLETION_REQUIREMENT")
    if close_on_merge and requirements:
        raise ValueError("CLOSE_ON_MERGE_CONFLICTS_WITH_POST_MERGE_REQUIREMENT")
    if not close_on_merge and not requirements:
        raise ValueError("MISSING_COMPLETION_CONTRACT")
    return {"version": 1, "close_on_merge": close_on_merge,
            "requirements": requirements}


def queue_metadata(body: str) -> tuple[int, tuple[int, ...]] | None:
    if not acceptance_flag(body, "AI_TEAM_AUTO_QUEUE"):
        return None
    priority = re.search(r"(?mi)^\s*AI_TEAM_QUEUE_PRIORITY\s*=\s*(-?\d+)\s*$", body)
    if not priority:
        return None
    depends = re.search(r"(?mi)^\s*AI_TEAM_DEPENDS_ON\s*=\s*([^\n]*)$", body)
    values: list[int] = []
    if depends and depends.group(1).strip():
        for value in depends.group(1).split(","):
            if not re.fullmatch(r"\s*#?\d+\s*", value):
                return None
            values.append(int(value.strip().lstrip("#")))
    return int(priority.group(1)), tuple(values)


def finalizes_parent(body: str) -> int | None:
    """Parse only the explicit trusted-child protocol marker."""
    matches = re.findall(
        r"(?mi)^\s*AI_TEAM_FINALIZES_PARENT\s*=\s*#?(\d+)\s*$", body or ""
    )
    if len(matches) != 1:
        return None
    parent = int(matches[0])
    return parent if parent > 0 else None


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
    phrases = (
        "rate limit", "usage limit", "quota exceeded", "too many requests",
        "limit reached", "you've hit your limit", "you’ve hit your limit",
        "weighted token", "weighted-token", "usage denied", "capacity unavailable",
        "provider unavailable", "status 429", "http 429", "status code 429",
    )
    if not any(p in low for p in phrases) and not re.search(r"\b429\b", low):
        return False, None
    now = dt.datetime.now(dt.timezone.utc)
    m = re.search(
        r"(?:try again|reset(?:s)?)(?:\s+in)?\s+(\d+(?:\.\d+)?)\s*"
        r"(second|minute|hour|day)s?", low,
    )
    if m:
        n = float(m.group(1))
        mult = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[m.group(2)]
        when = now + dt.timedelta(seconds=n * mult + 30)
    else:
        iso = re.search(
            r"(?:reset(?:s)?(?: at)?|retry(?:[_ -]?after)?(?: at)?)\s*[:=]?\s*"
            r"(\d{4}-\d\d-\d\d[t ]\d\d:\d\d(?::\d\d)?(?:z|[+-]\d\d:?\d\d)?)",
            low,
        )
        clock = re.search(r"reset(?:s)?(?:\s+at)?\s+(\d{1,2})(?::(\d\d))?\s*(am|pm)\b", low)
        parsed = parse_utc(iso.group(1).replace(" ", "T")) if iso else None
        if parsed:
            when = parsed.astimezone(dt.timezone.utc)
        elif clock:
            hour = int(clock.group(1)) % 12 + (12 if clock.group(3) == "pm" else 0)
            when = now.replace(hour=hour, minute=int(clock.group(2) or 0), second=0, microsecond=0)
            if when <= now:
                when += dt.timedelta(days=1)
        else:
            when = now + dt.timedelta(seconds=default_seconds)
    return True, when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def bounded_limit_text(text: str) -> str:
    """Persist enough provider context to diagnose a wait, without secrets/noise."""
    return re.sub(r"\s+", " ", bounded_redacted(text, 1200)).strip()[-1200:]


def extract_review(result: str, target_sha: str) -> tuple[str, list[Any], str]:
    verdict_m = re.search(r"(?mi)^\s*VERDICT\s*=\s*(PASS|FAIL)\s*$", result)
    sha_m = re.search(r"(?mi)^\s*REVIEWED_SHA\s*=\s*([0-9a-f]{40})\s*$", result)
    if not verdict_m or not sha_m:
        raise RuntimeError("reviewer did not emit required REVIEWED_SHA/VERDICT lines")
    if sha_m.group(1) != target_sha:
        raise RuntimeError(f"stale reviewer SHA {sha_m.group(1)} != {target_sha}")
    verdict = verdict_m.group(1)
    blockers: list[Any] = []
    b = re.search(r"(?mi)^\s*BLOCKERS_JSON\s*=\s*(\[.*\])\s*$", result)
    if b:
        try:
            raw = json.loads(b.group(1))
            if isinstance(raw, list) and all(isinstance(x, (str, dict)) for x in raw):
                blockers = raw
        except json.JSONDecodeError:
            pass
    if verdict == "FAIL" and not blockers:
        blockers = ["Reviewer returned FAIL; see full review comment/log for details."]
    summary = re.sub(r"\s+", " ", result).strip()[:700]
    return verdict, blockers, summary


def untracked_files(workdir: Path) -> list[str]:
    cp = git_worktree(
        workdir, "ls-files", "--others", "--exclude-standard", "--", check=True
    )
    return [x.strip() for x in cp.stdout.splitlines() if x.strip()]


def changed_files(workdir: Path, base_sha: str) -> list[str]:
    cp = git_worktree(workdir, "diff", "--name-only", base_sha, "--", check=True)
    tracked = [x.strip() for x in cp.stdout.splitlines() if x.strip()]
    return list(dict.fromkeys([*tracked, *untracked_files(workdir)]))


def change_scan_text(workdir: Path, base_sha: str) -> str:
    parts = [git_worktree(workdir, "diff", base_sha, "--", check=True).stdout]
    for name in untracked_files(workdir):
        path = workdir / name
        if path.is_symlink():
            raise RuntimeError(f"unsafe untracked symlink: {name}")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise RuntimeError(f"cannot inspect untracked file {name}: {exc}") from exc
        if size > 1_000_000:
            raise RuntimeError(f"untracked file too large for safety scan: {name}")
        try:
            parts.append(path.read_bytes().decode("utf-8", errors="replace"))
        except OSError as exc:
            raise RuntimeError(f"cannot read untracked file {name}: {exc}") from exc
    return "\n".join(parts)


def validate_changes(cfg: dict[str, Any], workdir: Path, base_sha: str,
                     expected_effect: str = "REPO_DIFF") -> tuple[list[str], bool]:
    files = changed_files(workdir, base_sha)
    if not files:
        if expected_effect == "NO_REPO_DIFF":
            return [], False
        raise RuntimeError("agent produced no file changes")
    no_auto = False
    for name in files:
        if name.startswith("/") or ".." in Path(name).parts:
            raise RuntimeError(f"unsafe changed path: {name}")
        if any(name.startswith(p) for p in cfg["safety"]["no_auto_merge_path_prefixes"]):
            # Only this tiny AI-control-plane allowlist can even be proposed by an
            # autonomous builder. All other protected/live-sensitive paths still
            # fail before commit/push. Merge is independently gated in handle_ci().
            if name not in AUTO_APPLY_CONTROL_PLANE_PATHS:
                raise RuntimeError(f"autonomous task touched owner-sensitive live path: {name}")
            no_auto = True
    diff = change_scan_text(workdir, base_sha)
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

    def kick_trello_reconciliation(self) -> None:
        """Start projection outside model workers; canonical work never waits for it."""
        if not TRELLO_BRIDGE.exists():
            return
        try:
            subprocess.Popen(
                [sys.executable, str(TRELLO_BRIDGE), "--reconcile-dir",
                 str(self.runtime.trello_outbox_dir), "--max-events", "50"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError as exc:
            self.runtime.event("TRELLO_RECONCILE_DEFERRED", error=type(exc).__name__)

    def emit_terminal_projection(self, task: sqlite3.Row, target_sha: str) -> None:
        """Queue terminal observability only after canonical completion is durable."""
        try:
            self.runtime.event(
                "COMPLETED",
                assignment_id=task["id"],
                issue=task["issue_number"],
                pr=task["pr_number"],
                target_sha=target_sha,
                status="DONE",
                result="merged and proven",
                next_action="Done / Proven",
            )
        except Exception:
            # The ledger-derived bridge reconciliation repairs a missing projection.
            # Trello/runtime observability must not turn a successful merge into failure.
            return

    def completion_contract(self, issue: dict[str, Any]) -> dict[str, Any]:
        if str(issue.get("author_association") or "") not in self.trusted:
            raise ValueError("UNTRUSTED_COMPLETION_CONTRACT")
        try:
            return parse_completion_contract(str(issue.get("body") or ""))
        except ValueError as exc:
            issue_number = issue.get("number")
            reconciled = (
                self.cfg.get("completion_reconciliation", {}).get(str(issue_number))
                if issue_number is not None
                else None
            )
            if not reconciled or str(exc) != "MISSING_COMPLETION_CONTRACT":
                raise
            if any(item not in COMPLETION_REQUIREMENTS for item in reconciled):
                raise ValueError("INVALID_RECONCILED_COMPLETION_CONTRACT") from exc
            return {"version": 1, "close_on_merge": False,
                    "requirements": list(reconciled), "source": "rollout_reconciliation"}

    def reconcile_completion_rollout(self) -> None:
        """Resume named reopened incidents from merged checkpoints, idempotently."""
        for raw_number in self.cfg.get("completion_reconciliation", {}):
            number = int(raw_number)
            if self.ledger.active_for_issue(number):
                continue
            try:
                issue = self.gh.issue(number)
                if str(issue.get("state") or "open").lower() != "open":
                    continue
                task = self.enqueue_acceptance(issue, parent_id=None)
                if task:
                    self.gh.add_labels(number, [self.cfg["labels"]["pending"]])
                    self.runtime.event("ROLLOUT_ACCEPTANCE_RECONCILED", issue=number,
                                       assignment_id=task["id"], task_type=task["task_type"],
                                       status="PENDING")
            except Exception as exc:
                self.runtime.event("ROLLOUT_ACCEPTANCE_RETRY", issue=number, error=str(exc))

    def enqueue_acceptance(self, issue: dict[str, Any], *, parent_id: str | None,
                           merged_sha: str | None = None) -> sqlite3.Row | None:
        """Create exactly the next unmet durable acceptance phase."""
        number = int(issue["number"])
        contract = self.completion_contract(issue)
        proven = self.ledger.proven_requirements(number)
        for requirement in contract["requirements"]:
            if requirement in proven:
                continue
            for phase in COMPLETION_REQUIREMENTS[requirement]:
                existing = self.ledger.phase_task(number, phase)
                if existing:
                    if str(existing["status"]) in ACTIVE_STATUSES:
                        return existing
                    if self.ledger.phase_is_proven(number, requirement, phase):
                        continue
                opus_phase = phase in {"DESTRUCTIVE_REVIEW", "EVIDENCE_AUDIT", "FINAL_VERDICT"}
                manager_phase = phase == "AUTHORIZED_APPLY"
                task_id = self.ledger.create_task(
                    issue_number=number, task_type=phase,
                    agent="TRUSTED_MANAGER" if manager_phase else (
                        "CLAUDE" if opus_phase else "CODEX_CHATGPT"),
                    model_class="NONE" if manager_phase else (
                        "OPUS" if opus_phase else "CODEX_DEFAULT"),
                    task_class="ROUTINE", status="PENDING", target_sha=merged_sha,
                    parent_id=parent_id, lifecycle_phase=phase,
                    completion_contract=contract, evidence={"requirement": requirement},
                )
                self.runtime.event(
                    "POST_MERGE_PHASE_ENQUEUED", assignment_id=task_id, issue=number,
                    target_sha=merged_sha, task_type=phase, lifecycle_phase=phase,
                    requirement=requirement, status="PENDING",
                    next_action=f"execute {phase.lower()} and record deterministic proof",
                )
                return self.ledger.get(task_id)
        return None

    def acceptance_is_proven(self, issue: dict[str, Any]) -> bool:
        contract = self.completion_contract(issue)
        return set(contract["requirements"]).issubset(
            self.ledger.proven_requirements(int(issue["number"]))
        )

    def finalize_proven_issue(self, issue: dict[str, Any], task: sqlite3.Row,
                              *, result: str = "acceptance predicate proven") -> None:
        if not self.acceptance_is_proven(issue):
            raise ValueError("ACCEPTANCE_NOT_PROVEN")
        number = int(issue["number"])
        labels = self.cfg["labels"]
        self.ledger.update(task["id"], status="DONE", lifecycle_phase="DONE",
                           retry_at=None, last_error=None)
        self.gh.add_labels(number, [labels["done"]])
        for stale in (labels["blocked"], labels["pending"], labels["ready"], labels["queued"]):
            self.gh.remove_label(number, stale)
        if str(issue.get("state") or "open").lower() != "closed":
            self.gh.close_issue(number)
        self.runtime.event("COMPLETED", assignment_id=task["id"], issue=number,
                           pr=task["pr_number"], target_sha=task["target_sha"],
                           status="DONE", result=result,
                           lifecycle_phase="DONE", next_action="Done / Proven")

    def complete_acceptance_phase(self, task: sqlite3.Row,
                                  evidence: dict[str, Any]) -> sqlite3.Row | None:
        """Atomic manager entry point used by deterministic phase runners."""
        issue = self.gh.issue(int(task["issue_number"]))
        saved = json.loads(task["evidence_json"] or "{}")
        requirement = str(saved.get("requirement") or "")
        contract = self.completion_contract(issue)
        if requirement not in contract["requirements"]:
            raise ValueError("EVIDENCE_REQUIREMENT_NOT_IN_CONTRACT")
        phase = str(task["lifecycle_phase"])
        if phase == "AUTHORIZED_APPLY":
            if not self.ledger.phase_is_proven(
                int(task["issue_number"]), requirement, "DESTRUCTIVE_REVIEW"
            ):
                raise ValueError("APPLY_REQUIRES_PROVEN_DESTRUCTIVE_REVIEW")
        _, predicate = self.ledger.record_acceptance_evidence(
            issue_number=int(task["issue_number"]), requirement=requirement,
            phase=phase, evidence=evidence,
        )
        if not predicate:
            repair_type = "RESEARCH" if requirement == "PROSPECTIVE_EVIDENCE" else "REPAIR"
            agent = "CLAUDE" if repair_type == "RESEARCH" else "CODEX_CHATGPT"
            model = "OPUS" if repair_type == "RESEARCH" else "CODEX_DEFAULT"
            self.ledger.update(task["id"], status="DONE", last_error="acceptance predicate failed",
                               lifecycle_phase="REPAIR")
            repair_id = self.ledger.create_task(
                issue_number=int(task["issue_number"]), task_type=repair_type, agent=agent,
                model_class=model, task_class=str(task["task_class"]), parent_id=str(task["id"]),
                target_sha=task["target_sha"], lifecycle_phase="REPAIR",
                completion_contract=contract, blockers=["acceptance predicate failed"],
            )
            self.runtime.event("ACCEPTANCE_REPAIR_ENQUEUED", assignment_id=repair_id,
                               issue=task["issue_number"], status="PENDING",
                               lifecycle_phase="REPAIR", requirement=requirement)
            return self.ledger.get(repair_id)
        self.ledger.update(task["id"], status="DONE", last_error=None)
        next_task = self.enqueue_acceptance(issue, parent_id=str(task["id"]),
                                            merged_sha=task["target_sha"])
        if next_task:
            return next_task
        self.ledger.update(task["id"], lifecycle_phase="PROVEN")
        self.runtime.event("ACCEPTANCE_PROVEN", assignment_id=task["id"],
                           issue=task["issue_number"], target_sha=task["target_sha"],
                           status="PROVEN", lifecycle_phase="PROVEN")
        self.finalize_proven_issue(issue, self.ledger.get(str(task["id"])))
        return None

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
            try:
                route = parse_initial_route(body, self.cfg)
                completion_contract = self.completion_contract(issue)
            except ValueError as exc:
                task_id = self.ledger.create_task(
                    issue_number=number, task_type="TERMINAL", agent="MANAGER",
                    model_class="NONE", task_class="UNCLASSIFIED", status="BLOCKED",
                    last_error=str(exc),
                )
                blocker = normalize_blocker({}, subject_sha="INITIAL", source_kind="ISSUE",
                                            source_id=str(number))
                blocker["rule_id"] = "INVALID_INITIAL_ROUTE"
                self.ledger.observe_remediation(blocker, issue_number=number, pr_number=None,
                                                actor="MANAGER", parent_assignment_id=task_id)
                self.block(self.ledger.get(task_id), str(exc))
                return True
            task_class = route["task_class"]
            task_id = self.ledger.create_task(
                issue_number=number,
                task_type=route["task_type"],
                agent=route["agent"],
                model_class=route["model_class"],
                task_class=task_class,
                lifecycle_phase="IMPLEMENTING",
                completion_contract=completion_contract,
            )
            self.gh.comment(
                number,
                assignment_marker(
                    task_id=task_id,
                    agent=route["agent"], task_type=route["task_type"],
                    model_class=route["model_class"],
                    task_class=task_class,
                    issue_number=number,
                ),
            )
            self.gh.add_labels(number, [self.cfg["labels"]["pending"]])
            self.gh.remove_label(number, label)
            self.gh.remove_label(number, self.cfg["labels"]["queued"])
            self.runtime.event(
                "TASK_ASSIGNED",
                assignment_id=task_id,
                issue=number,
                agent=route["agent"], task_type=route["task_type"],
            )
            self.sync_runtime_checkpoint()
            return True
        return False

    def promote_queued_issue(self) -> bool:
        """Promote exactly one explicit, dependency-satisfied pending issue."""
        if self.ledger.has_queue_claim_conflict():
            return False
        labels = self.cfg["labels"]
        blocked_labels = {labels["blocked"], labels["done"]}
        eligible: list[tuple[int, int, dict[str, Any]]] = []
        dependency_blockers: dict[int, list[int]] = {}
        for issue in self.gh.pending_issues(labels["queued"]):
            number = int(issue["number"])
            names = {str(x.get("name")) for x in issue.get("labels", [])}
            body = str(issue.get("body") or "")
            metadata = queue_metadata(body)
            if (
                metadata is None or names & blocked_labels
                or str(issue.get("author_association") or "") not in self.trusted
            ):
                continue
            priority, dependencies = metadata
            unresolved: list[int] = []
            for dependency in dependencies:
                try:
                    dep = self.gh.issue(dependency)
                except Exception:
                    unresolved.append(dependency)
                    continue
                dep_labels = {str(x.get("name")) for x in dep.get("labels", [])}
                if (
                    str(dep.get("state") or "open").lower() != "closed"
                    and labels["done"] not in dep_labels
                ):
                    unresolved.append(dependency)
            if not unresolved:
                eligible.append((priority, number, issue))
            else:
                dependency_blockers[number] = unresolved
        if not eligible:
            if dependency_blockers:
                fingerprint = json.dumps(dependency_blockers, sort_keys=True)
                if self.ledger.meta_get("queue_dependency_blockers") != fingerprint:
                    self.ledger.meta_set("queue_dependency_blockers", fingerprint)
                    self.runtime.event(
                        "QUEUE_DEPENDENCY_BLOCKED", blockers=dependency_blockers,
                        status="IDLE_DEPENDENCY_BLOCKED",
                    )
                    self.sync_runtime_checkpoint()
            return False
        self.ledger.meta_set("queue_dependency_blockers", "")
        _, number, _issue = min(eligible, key=lambda row: (row[0], row[1]))
        self.gh.add_labels(number, [labels["ready"]])
        self.runtime.event("QUEUE_PROMOTED", issue=number)
        return self.claim_ready_issue()

    def reconcile_parent_finalizers(self) -> bool:
        """Finalize parents only for trusted, canonically successful children."""
        changed = False
        try:
            children = self.gh.finalizer_issues(self.cfg["labels"]["done"])
        except Exception as exc:
            self.runtime.event("PARENT_FINALIZE_RETRY", error=str(exc))
            return False
        for child in children:
            try:
                changed = self._reconcile_parent_finalizer(child) or changed
            except Exception as exc:
                self.runtime.event(
                    "PARENT_FINALIZE_RETRY", child_issue=child.get("number"),
                    error=str(exc),
                )
        if changed:
            self.sync_runtime_checkpoint()
            self.kick_trello_reconciliation()
        return changed

    def _reconcile_parent_finalizer(self, child: dict[str, Any]) -> bool:
        labels = self.cfg["labels"]
        child_number = int(child["number"])
        parent_number = finalizes_parent(str(child.get("body") or ""))
        child_labels = {str(x.get("name")) for x in child.get("labels", [])}
        if (
            parent_number is None
            or parent_number == child_number
            or str(child.get("author_association") or "") not in self.trusted
            or str(child.get("state") or "open").lower() != "closed"
            or labels["done"] not in child_labels
            or not self.ledger.successful_issue(child_number)
        ):
            return False
        key = f"parent_finalized:{child_number}:{parent_number}"
        if self.ledger.meta_get(key):
            return False
        parent = self.gh.issue(parent_number)
        parent_labels = {str(x.get("name")) for x in parent.get("labels", [])}
        try:
            parent_proven = self.acceptance_is_proven(parent)
        except ValueError as exc:
            self.runtime.event("PARENT_ACCEPTANCE_BLOCKED", issue=parent_number,
                               child_issue=child_number, status="BLOCKED", error=str(exc))
            return False
        if not parent_proven:
            continuation = self.enqueue_acceptance(
                parent, parent_id=None, merged_sha=str(child.get("target_sha") or "") or None
            )
            if continuation:
                self.gh.add_labels(parent_number, [labels["pending"]])
                self.gh.remove_label(parent_number, labels["done"])
                self.runtime.event(
                    "PARENT_ACCEPTANCE_CONTINUED", issue=parent_number,
                    child_issue=child_number, assignment_id=continuation["id"],
                    task_type=continuation["task_type"], status="PENDING",
                    next_action=f"execute {str(continuation['task_type']).lower()}",
                )
                return True
            return False
        self.gh.add_labels(parent_number, [labels["done"]])
        for stale in (
            labels["blocked"], labels["pending"], labels["ready"], labels["queued"]
        ):
            if stale in parent_labels:
                self.gh.remove_label(parent_number, stale)
        if str(parent.get("state") or "open").lower() != "closed":
            self.gh.close_issue(parent_number)
        self.runtime.event(
            "PARENT_FINALIZED", issue=parent_number, child_issue=child_number,
            status="DONE", result="trusted child completed",
            next_action="Done / Proven",
        )
        self.ledger.meta_set(key, utcnow())
        return True

    def reconcile_handoffs(self) -> None:
        """Recover a child task if a restart/API failure interrupted a handoff."""
        for row in self.ledger.handoff_candidates():
            try:
                if row["task_type"] in {"BUILD", "REPAIR"}:
                    if not self.ledger.child(str(row["id"]), "REVIEW"):
                        self.enqueue_review(
                            row, int(row["pr_number"]), str(row["target_sha"])
                        )
                        self.runtime.event(
                            "HANDOFF_RECOVERED",
                            assignment_id=row["id"],
                            issue=row["issue_number"],
                            pr=row["pr_number"],
                            child_type="REVIEW",
                        )
                elif row["status"] == "DONE":
                    if not self.ledger.child(str(row["id"]), "REPAIR"):
                        blockers = json.loads(row["blockers_json"] or "[]")
                        self.dispatch_remediations(row, blockers, source_kind="REVIEW",
                                                   source_id=str(row["id"]))
                        self.runtime.event(
                            "HANDOFF_RECOVERED",
                            assignment_id=row["id"],
                            issue=row["issue_number"],
                            pr=row["pr_number"],
                            child_type="REPAIR",
                        )
                elif row["status"] == "STALE" and row["pr_number"]:
                    pr = self.gh.pr(int(row["pr_number"]))
                    if str(pr.get("state") or "open").lower() != "open":
                        continue
                    current_sha = str(pr["head"]["sha"])
                    if current_sha == str(row["target_sha"]):
                        continue
                    if not self.ledger.child(str(row["id"]), "REVIEW", current_sha):
                        self.enqueue_replacement_review(row, current_sha)
                        self.runtime.event(
                            "HANDOFF_RECOVERED",
                            assignment_id=row["id"],
                            issue=row["issue_number"],
                            pr=row["pr_number"],
                            child_type="REVIEW",
                            target_sha=current_sha,
                        )
            except Exception as exc:
                self.runtime.event(
                    "HANDOFF_RECOVERY_RETRY",
                    assignment_id=row["id"],
                    issue=row["issue_number"],
                    pr=row["pr_number"],
                    error=str(exc),
                )

    def migrate_legacy_remediation(self) -> None:
        """One-shot, restart-safe projection of legacy #166/#168/#170 assignments."""
        migration = self.cfg.get("legacy_remediation_migration", {})
        version = str(migration.get("version", 1))
        key = f"remediation_migration_v{version}"
        if self.ledger.meta_get(key) == "DONE":
            return
        issues = tuple(int(x) for x in migration.get("issues", (166, 168, 170)))
        placeholders = ",".join("?" for _ in issues)
        now = utcnow()
        self.ledger.db.execute(
            f"UPDATE tasks SET status='STALE',last_error='SUPERSEDED_BY_REMEDIATION_V1',"
            f"retry_at=NULL,updated_at=? WHERE issue_number IN ({placeholders}) "
            "AND status IN "
            "('PENDING','RETRY','WAITING_RATE_LIMIT','WAITING_CI','RUNNING','BLOCKED')",
            (now, *issues),
        )
        self.ledger.db.commit()
        # The snapshot is authoritative input for the deployed manager. Missing API
        # evidence is recorded per issue and never expands into unrelated queue blocking.
        for number in issues:
            marker = f"legacy_reconciled_v{version}:{number}"
            if self.ledger.meta_get(marker):
                continue
            try:
                issue = self.gh.issue(number)
                snapshot = {"state": issue.get("state"),
                            "labels": sorted(str(x.get("name")) for x in issue.get("labels", [])),
                            "authorization": {
                                "author_association": issue.get("author_association"),
                                "protected_change": acceptance_flag(
                                    str(issue.get("body") or ""), "AI_TEAM_PROTECTED_CHANGE"
                                ),
                            }}
                pr_number = {166: 167, 168: 169, 170: 171}[number]
                try:
                    pr = self.gh.pr(pr_number)
                    head = str((pr.get("head") or {}).get("sha") or "")
                    snapshot["pr"] = {"number": pr_number, "state": pr.get("state"),
                                      "head": head,
                                      "checks": self.gh.check_state(head) if head else None}
                    snapshot["machine_reviews"] = [
                        c.get("body") for c in self.gh.comments(pr_number)
                        if MACHINE_RESULT in str(c.get("body") or "")
                    ]
                except Exception as exc:
                    snapshot["pr_error"] = type(exc).__name__
            except Exception as exc:
                snapshot = {"error": type(exc).__name__}
            self.ledger.meta_set(marker, canonical_json(snapshot))
            if "error" in snapshot:
                blocker = normalize_blocker({}, subject_sha="LEGACY", source_kind="ISSUE",
                                            source_id=str(number))
                self.ledger.observe_remediation(
                    blocker, issue_number=number, pr_number=None, actor="MANAGER"
                )
        # #170/#171 are explicitly superseded by the accepted diagnosis; this is
        # idempotent against already-closed GitHub objects.
        try:
            self.gh.api("PATCH", f"repos/{REPO}/pulls/171", {"state": "closed"})
            self.gh.close_issue(int(migration.get("supersede_issue", 170)))
        except Exception as exc:
            self.runtime.event("LEGACY_SUPERSEDE_RETRY", issue=170, pr=171, error=str(exc))
            return
        # Restore #120 only when its own declared dependency closure is satisfied.
        release = int(migration.get("release_issue", 120))
        try:
            candidate = self.gh.issue(release)
            metadata = queue_metadata(str(candidate.get("body") or ""))
            dependencies = metadata[1] if metadata else ()
            satisfied = True
            for dep in dependencies:
                dependency = self.gh.issue(dep)
                dep_labels = {str(x.get("name")) for x in dependency.get("labels", [])}
                if (str(dependency.get("state") or "open").lower() != "closed"
                        and self.cfg["labels"]["done"] not in dep_labels):
                    satisfied = False
                    break
            if satisfied:
                self.gh.add_labels(release, [self.cfg["labels"]["queued"]])
                self.gh.remove_label(release, self.cfg["labels"]["blocked"])
        except Exception as exc:
            self.runtime.event("LEGACY_RELEASE_RETRY", issue=release, error=str(exc))
            return
        self.ledger.meta_set(key, "DONE")

    def cycle(self) -> None:
        for stale in self.ledger.recover_interrupted():
            self.reap_stale_child(stale)
            self.runtime.event(
                "STALE_RUN_REQUEUED", assignment_id=stale["id"],
                issue=stale["issue_number"], pr=stale["pr_number"],
                target_sha=stale["target_sha"], session_id=stale["session_id"],
            )
        self.migrate_legacy_remediation()
        self.reconcile_completion_rollout()
        self.reconcile_handoffs()
        if self.ledger.due() is not None:
            self.reconcile_parent_finalizers()
        self.sync_runtime_checkpoint()
        self.kick_trello_reconciliation()
        task = self.ledger.due()
        if task is None:
            if not self.claim_ready_issue():
                self.reconcile_parent_finalizers()
                if not self.claim_ready_issue():
                    self.promote_queued_issue()
            task = self.ledger.due()
        if task is None:
            return
        try:
            if task["status"] == "WAITING_CI":
                self.handle_ci(task)
            elif task["status"] == "WAITING_RATE_LIMIT" and task["agent"] == "CLAUDE":
                self.handle_claude_probe(task)
            elif task["task_type"] in EVIDENCE_TASK_TYPES:
                # Evidence runners deposit a complete envelope durably before this
                # transition. A missing result remains runnable and never becomes Done.
                payload = json.loads(task["evidence_json"] or "{}")
                if isinstance(payload.get("result"), dict):
                    self.complete_acceptance_phase(task, payload["result"])
                else:
                    retry_at = retry_at_after(max(60, int(self.cfg["poll_seconds"])))
                    self.ledger.update(task["id"], status="RETRY", retry_at=retry_at,
                                       last_error="awaiting deterministic phase runner evidence")
                    self.runtime.event("ACCEPTANCE_PHASE_READY", assignment_id=task["id"],
                                       issue=task["issue_number"], task_type=task["task_type"],
                                       status="RETRY", retry_after=retry_at)
            elif task["task_type"] in {"BUILD", "REPAIR"}:
                self.handle_codex(task)
            elif task["task_type"] == "REVIEW":
                self.handle_review(task)
            elif task["task_type"] == "RESEARCH":
                self.handle_research(task)
            else:
                self.block(task, f"unsupported task type {task['task_type']}")
        finally:
            self.sync_runtime_checkpoint()
            self.kick_trello_reconciliation()

    def reap_stale_child(self, task: dict[str, Any] | sqlite3.Row) -> None:
        unit = task["systemd_unit"]
        if not unit or not re.fullmatch(
            r"hl-ai-(?:claude|codex)(?:-probe)?-[A-Za-z0-9-]+", str(unit)
        ):
            return
        run(["systemctl", "stop", str(unit)], timeout=15)

    def handle_claude_probe(self, task: sqlite3.Row) -> None:
        """Make a bounded, repo-free availability check; never spend an attempt."""
        unit = f"hl-ai-claude-probe-{task['id'][:10]}-{int(time.time())}"
        command = [
            "/usr/bin/claude", "-p", "--model",
            "opus" if task["model_class"] == "OPUS" else "sonnet",
            "--output-format", "json", "--permission-mode", "dontAsk", "--max-turns", "1",
        ]
        full = model_sandbox_command(
            unit=unit, user=CLAUDE_USER, home=CLAUDE_HOME,
            workdir=STATE_ROOT / "orchestrator", command=command,
        )
        timeout = int(self.cfg["claude_readiness_probe_timeout_seconds"])
        try:
            cp = run(full, input_text="Reply with exactly CLAUDE_READY_OK", timeout=timeout)
            combined = (cp.stdout + "\n" + cp.stderr)[
                -int(self.cfg["claude_readiness_probe_output_bytes"]):
            ]
        except subprocess.TimeoutExpired as exc:
            self.reap_stale_child({"systemd_unit": unit})
            combined = str(exc)
            cp = subprocess.CompletedProcess(full, 124, "", combined)
        limited, parsed_retry = rate_limit_info(
            combined, int(self.cfg["claude_readiness_probe_seconds"])
        )
        ready = cp.returncode == 0 and "CLAUDE_READY_OK" in combined
        if ready:
            self.ledger.update(
                task["id"], status="PENDING", retry_at=utcnow(), last_error=None,
                limit_text=None, systemd_unit=None,
            )
            self.runtime.event("CLAUDE_READINESS_PROBE_READY", assignment_id=task["id"])
            return
        detail = bounded_limit_text(combined) or f"probe rc={cp.returncode}"
        if limited:
            retry_at = parsed_retry or retry_at_after(
                int(self.cfg["claude_readiness_probe_seconds"])
            )
            self.ledger.update(
                task["id"], status="WAITING_RATE_LIMIT", retry_at=retry_at,
                last_error="Claude provider still unavailable", limit_text=detail,
                systemd_unit=None,
            )
            self.runtime.event(
                "CLAUDE_READINESS_PROBE_WAITING", assignment_id=task["id"],
                retry_after=retry_at, limit_text=detail, detected_limit=True,
            )
            return

        next_attempt = int(task["attempt"]) + 1
        self.ledger.update(
            task["id"], attempt=next_attempt, limit_text=None, systemd_unit=None
        )
        current = self.ledger.get(task["id"])
        error = (
            f"Claude readiness probe ordinary failure rc={cp.returncode}: "
            f"{detail[:800]}"
        )
        self.retry_or_block(current, error, session_id=task["session_id"])
        updated = self.ledger.get(task["id"])
        self.runtime.event(
            "CLAUDE_READINESS_PROBE_FAILURE",
            assignment_id=task["id"], issue=task["issue_number"],
            pr=task["pr_number"], attempt=next_attempt,
            status=updated["status"], retry_after=updated["retry_at"],
            detail=detail[:800],
        )

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
            if task["pr_number"]:
                pr = self.gh.pr(int(task["pr_number"]))
                branch = str(pr["head"]["ref"])
                base_ref = branch
            else:
                # A failed audit after async merge repairs/rolls back from current main
                # in a fresh PR; the already-merged PR cannot be reused.
                base_ref = "origin/main"
                branch = task["branch"] or f"codex/repair-{task['issue_number']}-{task['id'][:8]}"
        workdir = prepare_checkout(
            user=CODEX_USER,
            home=CODEX_HOME,
            base_dir=CODEX_WORK,
            task_id=str(task["id"]),
            ref=base_ref,
            branch=branch,
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
        self.ledger.update(task["id"], systemd_unit=unit)
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
                limit_text = bounded_limit_text(combined)
                self.ledger.update(
                    task["id"],
                    status="WAITING_RATE_LIMIT",
                    retry_at=retry_at,
                    session_id=session_id,
                    attempt=max(0, int(task["attempt"]) - 1),
                    limit_text=limit_text,
                    systemd_unit=None,
                    last_error="Codex rate/usage limit",
                )
                self.runtime.event(
                    "CODEX_WAITING_RATE_LIMIT",
                    assignment_id=task["id"],
                    issue=task["issue_number"],
                    pr=task["pr_number"],
                    session_id=session_id,
                    retry_after=retry_at,
                    limit_text=limit_text,
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
                systemd_unit=None,
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
            if task["task_type"] == "BUILD" or not task["pr_number"]:
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
                systemd_unit=None,
            )
            remediation = self.ledger.db.execute(
                "SELECT remediation_id FROM remediations WHERE parent_assignment_id=? "
                "AND class='CODE_CHANGE' ORDER BY created_at DESC LIMIT 1",
                (str(task["parent_id"]),),
            ).fetchone()
            if remediation:
                self.ledger.update_remediation(
                    str(remediation["remediation_id"]), status="COMPLETED",
                    completion_evidence=canonical_json({"new_sha": new_sha}),
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
            error = str(exc)
            current = self.ledger.get(task["id"])
            if task["task_type"] == "REPAIR" and error == "agent produced no file changes":
                remediation = self.ledger.db.execute(
                    "SELECT remediation_id FROM remediations WHERE parent_assignment_id=? "
                    "AND class='CODE_CHANGE' ORDER BY created_at DESC LIMIT 1",
                    (str(task["parent_id"]),),
                ).fetchone()
                if remediation:
                    self.ledger.update_remediation(
                        str(remediation["remediation_id"]), status="TERMINAL",
                        completion_evidence="completed code action produced no progress",
                    )
                self.block(current, "unchanged CODE_CHANGE fingerprint after one completed action")
                self.finish_runtime_run(
                    run_id, str(task["id"]), stdout=cp.stdout, stderr=cp.stderr,
                    exit_code=1, session_id=session_id, usage=usage, result=result,
                    error=error, status="BLOCKED",
                )
                return
            fail_closed_markers = (
                "unsafe changed path",
                "owner-sensitive live path",
                "forbidden live-trading enablement",
                "unsafe untracked symlink",
                "untracked file too large for safety scan",
            )
            if any(marker in error for marker in fail_closed_markers):
                self.block(current, error)
            else:
                self.retry_or_block(
                    current,
                    f"Codex postprocess/finalize failed: {error}",
                    session_id=session_id,
                )
                updated = self.ledger.get(task["id"])
                if updated["status"] != "BLOCKED":
                    self.runtime.event(
                        "CODEX_POSTPROCESS_RETRY_SCHEDULED",
                        assignment_id=task["id"],
                        issue=task["issue_number"],
                        pr=task["pr_number"],
                        retry_after=updated["retry_at"],
                        error=error,
                    )
            updated = self.ledger.get(task["id"])
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=1,
                session_id=session_id,
                usage=usage,
                result=result,
                error=error,
                status=str(updated["status"]),
            )

    def invoke_codex(
        self, task: sqlite3.Row, workdir: Path, prompt: str, unit: str
    ) -> subprocess.CompletedProcess[str]:
        command: list[str]
        if task["session_id"]:
            command = [
                "/usr/local/bin/codex",
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "resume",
                str(task["session_id"]),
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
                + "\n".join(
                    f"- {canonical_json(x) if isinstance(x, dict) else x}" for x in blockers
                )
            )
        elif task["task_type"] == "BUILD" and blockers:
            repair = (
                "\nIMMUTABLE OPUS RESEARCH INPUT:\n"
                + "\n".join(
                    canonical_json(x) if isinstance(x, dict) else str(x) for x in blockers
                )
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
        uid, gid = normalize_worktree_ownership(workdir, CODEX_USER)
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
Refs #{issue["number"]}

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
        if self.ledger.child(str(parent["id"]), "REVIEW"):
            return
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
        try:
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
        except Exception as exc:
            self.runtime.event(
                "HANDOFF_MIRROR_FAILED", assignment_id=task_id,
                issue=parent["issue_number"], pr=pr_number,
                child_type="REVIEW", error=str(exc),
            )

    def handle_research(self, task: sqlite3.Row) -> None:
        """Run Opus research and hand its immutable result to a Codex BUILD child."""
        issue = self.gh.issue(int(task["issue_number"]))
        if task["agent"] != "CLAUDE" or task["model_class"] != "OPUS":
            self.block(task, "INVALID_INITIAL_ROUTE: research must be CLAUDE/OPUS")
            return
        workdir = prepare_checkout(user=CLAUDE_USER, home=CLAUDE_HOME, base_dir=CLAUDE_WORK,
                                   task_id=str(task["id"]), ref="origin/main")
        self.ledger.update(task["id"], status="RUNNING", workdir=str(workdir),
                           attempt=int(task["attempt"]) + 1)
        task = self.ledger.get(str(task["id"]))
        prompt = (
            f"Research GitHub Issue #{task['issue_number']} as CLAUDE OPUS. Produce an "
            "implementation-ready architecture artifact; do not edit files or use GitHub. "
            "Use only the Issue-linked files and the minimum necessary adjacent context. "
            "Do not recursively inspect or reread the repository. REAL TRADING remains "
            "disabled. Issue body:\n" + str(issue.get("body") or "")
        )
        unit = f"hl-ai-claude-{task['id'][:10]}-{int(time.time())}"
        log_path = CLAUDE_LOG / f"{task['id']}-attempt-{task['attempt']}.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        run_id = self.ledger.open_run(task, log_path)
        try:
            cp = self.invoke_claude(task, workdir, prompt, unit)
        except subprocess.TimeoutExpired:
            self.retry_or_block(task, "Opus research timeout")
            return
        combined = cp.stdout + "\n" + cp.stderr
        session_id, usage, result = parse_claude_output(cp.stdout)
        limited, retry_at = rate_limit_info(
            combined, int(self.cfg["default_rate_limit_retry_seconds"])
        )
        if limited:
            self.ledger.update(task["id"], status="WAITING_RATE_LIMIT", retry_at=retry_at,
                               session_id=session_id, attempt=max(0, int(task["attempt"]) - 1),
                               last_error="Claude rate/usage limit")
            return
        if cp.returncode or not result:
            self.retry_or_block(
                task, f"Opus research failed rc={cp.returncode}", session_id=session_id
            )
            return
        result_id = hashlib.sha256(result.encode()).hexdigest()
        evidence = {"research_result_id": result_id, "artifact": result}
        self.ledger.update(task["id"], status="DONE", session_id=session_id,
                           blockers_json=canonical_json(evidence), last_error=None)
        if not self.ledger.child(str(task["id"]), "BUILD"):
            self.ledger.create_task(
                issue_number=int(task["issue_number"]), task_type="BUILD",
                agent="CODEX_CHATGPT", model_class="CODEX_DEFAULT",
                task_class=str(task["task_class"]), blockers=[evidence],
                parent_id=str(task["id"]),
            )
        self.ledger.close_run(run_id, exit_code=0, session_id=session_id, usage=usage,
                              result=result, error=None)

    def handle_review(self, task: sqlite3.Row) -> None:
        if not task["pr_number"] or not task["target_sha"]:
            self.block(task, "review missing PR/SHA")
            return
        pr = self.gh.pr(int(task["pr_number"]))
        if str(pr.get("state") or "open").lower() != "open" and not pr.get("merged_at"):
            self.ledger.update(task["id"], status="STALE", retry_at=None,
                               last_error="PR is no longer open", systemd_unit=None)
            self.runtime.event(
                "OBSOLETE_REVIEW_DROPPED", assignment_id=task["id"], pr=task["pr_number"],
                target_sha=task["target_sha"],
            )
            return
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
        self.ledger.update(task["id"], systemd_unit=unit)
        self.runtime.run_started(run_id, task, prompt=prompt, systemd_unit=unit)
        self.sync_runtime_checkpoint()
        try:
            cp = self.invoke_claude(task, workdir, prompt, unit)
        except subprocess.TimeoutExpired as exc:
            self.reap_stale_child({"systemd_unit": unit})
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
        resume_session = session_id or task["session_id"]
        limited, retry_at = rate_limit_info(
            combined, int(self.cfg["claude_readiness_probe_seconds"])
        )
        self.ledger.close_run(
            run_id,
            exit_code=cp.returncode,
            session_id=session_id,
            usage=usage,
            result=result,
            error=None if cp.returncode == 0 else combined[-1500:],
        )
        if limited:
            self.ledger.update(
                task["id"], status="WAITING_RATE_LIMIT", retry_at=retry_at,
                session_id=resume_session, attempt=max(0, int(task["attempt"]) - 1),
                limit_text=bounded_limit_text(combined), systemd_unit=None,
                last_error="Claude rate/usage limit",
            )
            self.runtime.event(
                "CLAUDE_WAITING_RATE_LIMIT", assignment_id=task["id"],
                issue=task["issue_number"], pr=task["pr_number"], target_sha=target_sha,
                session_id=resume_session, retry_after=retry_at,
                limit_text=bounded_limit_text(combined),
            )
            self.finish_runtime_run(
                run_id, str(task["id"]), stdout=cp.stdout, stderr=cp.stderr,
                exit_code=cp.returncode, session_id=resume_session, usage=usage,
                result=result, error="Claude rate/usage limit",
            )
            return
        if cp.returncode != 0:
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
                session_id=resume_session,
                attempt=max(0, int(task["attempt"]) - 1),
                systemd_unit=None,
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
            self.ledger.update(
                task["id"], status="STALE", last_error="PR changed during review",
                systemd_unit=None,
            )
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
                systemd_unit=None,
            )
            self.dispatch_remediations(task, blockers, source_kind="REVIEW",
                                       source_id=str(task["id"]))
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
                systemd_unit=None,
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
        task_type = str(task["task_type"])
        turn_budget = int(self.cfg["claude_turn_budgets"][task_type])
        command.extend(["--max-turns", str(turn_budget)])
        full = model_sandbox_command(
            unit=unit,
            user=CLAUDE_USER,
            home=CLAUDE_HOME,
            workdir=workdir,
            command=command,
        )
        timeout_key = (
            "research_timeout_seconds" if task_type == "RESEARCH" else "review_timeout_seconds"
        )
        return run(full, input_text=prompt, timeout=int(self.cfg[timeout_key]))

    def review_prompt(
        self,
        pr: dict[str, Any],
        task: sqlite3.Row,
        changed: list[str],
        comments: list[str],
        blockers: list[str],
    ) -> str:
        target = str(task["target_sha"])
        base = str((pr.get("base") or {}).get("sha") or "")
        blocker_text = "\n".join(
            "- " + (canonical_json(x) if isinstance(x, dict) else str(x)) for x in blockers
        ) if blockers else "(none)"
        delta_start = str(task["previous_sha"] or base)
        delta = (
            f"\nReview ONLY the bounded delta `git diff {delta_start}..{target} -- "
            "<changed files>`, the prior blockers, and necessary adjacent context. "
            "Do not inspect unchanged files unless a specific changed-file finding requires it. "
            "Never perform or restart a recursive/repository-wide audit.\n"
        )
        if task["previous_sha"]:
            delta = (
                f"\nThis is a re-review. Previous reviewed SHA: {task['previous_sha']}. "
                f"Review the prior blockers plus ONLY the delta "
                f"`git diff {task['previous_sha']}..{target} -- <changed files>` and "
                "necessary adjacent context. Never perform or restart a recursive/"
                "repository-wide audit.\n"
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
2. Read the PR body below and only the changed files listed below, starting from the
   explicit base/previous-reviewed-SHA delta. Do not reread the whole repository.
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
{blocker_text}

LATEST TRUSTED PR COMMENTS:
{chr(10).join(comments[-6:]) if comments else "(none)"}

Run narrow relevant tests if useful. At the very end emit EXACTLY these machine-readable lines:
REVIEWED_SHA={target}
VERDICT=PASS
BLOCKERS_JSON=[]

If any merge-blocking defect exists, instead emit:
REVIEWED_SHA={target}
VERDICT=FAIL
BLOCKERS_JSON=[{{"protocol_version":1,"class":"CODE_CHANGE",\
"source_kind":"REVIEW","source_id":"{task['id']}","subject_sha":"{target}",\
"rule_id":"stable_rule_id","observed":{{"paths":["path"],"reproducer":"command"}},\
"requested_action":{{"paths":["path"],"reproducer":"command"}}}}]

The reviewed SHA must be exactly the target SHA.
Each blocker must be exactly one BLOCKER_V1 object. Use PR_METADATA, PROTECTED_ACTION,
CI_RETRY, REVIEW_RERUN, POLICY_RECONCILIATION, or TERMINAL when appropriate. Never infer
that non-code work needs a CODE_CHANGE. Unknown or contradictory evidence is TERMINAL.
"""

    def dispatch_remediations(self, parent: sqlite3.Row, blockers: list[Any], *,
                              source_kind: str, source_id: str) -> None:
        """Persist then execute typed, idempotent manager/model actions."""
        for raw in blockers:
            effective_source = str(raw.get("source_id")) if isinstance(raw, dict) else source_id
            blocker = normalize_blocker(raw, subject_sha=str(parent["target_sha"] or ""),
                                        source_kind=source_kind, source_id=effective_source)
            klass = blocker["class"]
            actor = self.cfg["remediation"]["actors"][klass]
            row = self.ledger.observe_remediation(
                blocker, issue_number=int(parent["issue_number"]),
                pr_number=int(parent["pr_number"]) if parent["pr_number"] else None,
                actor=actor, parent_assignment_id=str(parent["id"]),
            )
            # An existing action is an observation only: never spend or duplicate.
            if row["status"] != "OBSERVED" or int(row["occurrence_count"]) > 1:
                continue
            action = json.loads(row["requested_action_json"] or "{}")
            rid = str(row["remediation_id"])
            if klass == "CODE_CHANGE":
                self.enqueue_repair(parent, [blocker], action_key=str(row["action_key"]))
                self.ledger.update_remediation(rid, status="ACTION_STARTED", action_attempts=1,
                                               last_action_at=utcnow())
            elif klass == "PR_METADATA":
                allowed = {"title", "body", "base", "draft"}
                fields = action.get("fields")
                if not isinstance(fields, dict) or not fields or not set(fields) <= allowed:
                    self.ledger.update_remediation(rid, status="TERMINAL",
                                                   completion_evidence="invalid metadata action")
                    continue
                result = self.gh.patch_pr(int(parent["pr_number"]), fields)
                complete = all(result.get(k) == v for k, v in fields.items())
                self.ledger.update_remediation(rid, status="COMPLETED" if complete else "TERMINAL",
                                               action_attempts=1, last_action_at=utcnow(),
                                               completion_evidence=canonical_json(result))
                if complete:
                    self.ledger.update(parent["id"], status="WAITING_CI", retry_at=utcnow(),
                                       last_error=None)
            elif klass == "CI_RETRY":
                check_run = action.get("check_run_id")
                if not isinstance(check_run, int):
                    self.ledger.update_remediation(rid, status="TERMINAL",
                                                   completion_evidence="missing check_run_id")
                    continue
                self.gh.rerun_check(check_run)
                self.ledger.update_remediation(rid, status="COMPLETED", action_attempts=1,
                                               last_action_at=utcnow(),
                                               completion_evidence=canonical_json(
                                                   {"check_run_id": check_run}
                                               ))
                self.ledger.update(parent["id"], status="WAITING_CI", retry_at=utcnow())
            elif klass == "REVIEW_RERUN":
                self.enqueue_replacement_review(parent, str(parent["target_sha"]))
                self.ledger.update_remediation(rid, status="COMPLETED", action_attempts=0,
                                               completion_evidence="exact-SHA review requested")
            elif klass == "POLICY_RECONCILIATION":
                self.ledger.update_remediation(rid, status="COMPLETED", action_attempts=1,
                                               last_action_at=utcnow(),
                                               completion_evidence=(
                                                   "authoritative projection reconciled"
                                               ))
            elif klass == "PROTECTED_ACTION":
                name = action.get("name")
                protected = (self.cfg["remediation"].get("protected_actions", {}).get(name)
                             if isinstance(name, str) else None)
                issue = self.gh.issue(int(parent["issue_number"]))
                auth = parse_protected_action_authorization(str(issue.get("body") or ""))
                expiry = parse_utc(auth.get("expires_at")) if auth else None
                prior_uses = 0
                if auth:
                    for used in self.ledger.db.execute(
                        "SELECT completion_evidence FROM remediations "
                        "WHERE class='PROTECTED_ACTION' AND status='COMPLETED'"
                    ):
                        try:
                            evidence = json.loads(used["completion_evidence"] or "{}")
                        except json.JSONDecodeError:
                            continue
                        prior_uses += evidence.get("authorization_id") == auth["id"]
                valid = (
                    str(issue.get("author_association") or "") in self.trusted
                    and auth is not None and auth.get("id")
                    and auth.get("action") == name
                    and auth.get("subject_sha") == parent["target_sha"]
                    and isinstance(auth.get("max_actions"), int) and auth["max_actions"] >= 1
                    and prior_uses < auth["max_actions"]
                    and expiry is not None and expiry > dt.datetime.now(dt.timezone.utc)
                )
                if not valid or not isinstance(protected, dict):
                    self.ledger.update_remediation(rid, status="TERMINAL",
                                                   completion_evidence=(
                                                       "missing/invalid authorization"
                                                   ))
                    self.block(parent, "PROTECTED_ACTION missing/invalid repository authorization")
                else:
                    workflow = protected.get("workflow_id")
                    ref = protected.get("ref")
                    if not isinstance(workflow, str) or not isinstance(ref, str):
                        self.ledger.update_remediation(
                            rid, status="TERMINAL",
                            completion_evidence="authorized action missing workflow/ref",
                        )
                        continue
                    self.gh.dispatch_workflow(
                        workflow, ref,
                        {"authorization_id": str(auth["id"]),
                         "action_key": str(row["action_key"])},
                    )
                    self.ledger.update_remediation(
                        rid, status="COMPLETED", action_attempts=1,
                        last_action_at=utcnow(), completion_evidence=canonical_json(
                            {"authorization_id": auth["id"], "workflow": workflow,
                             "ref": ref}
                        ),
                    )
            else:
                self.ledger.update_remediation(rid, status="TERMINAL",
                                               completion_evidence=blocker["rule_id"])
                self.block(parent, blocker["rule_id"])

    def enqueue_repair(self, review: sqlite3.Row, blockers: list[Any],
                       action_key: str | None = None) -> None:
        if action_key and self.ledger.db.execute(
            "SELECT 1 FROM tasks WHERE parent_id=? AND blockers_json LIKE ? LIMIT 1",
            (str(review["id"]), f"%{action_key}%"),
        ).fetchone():
            return
        if self.ledger.child(str(review["id"]), "REPAIR"):
            return
        payload: list[Any] = blockers + ([{"action_key": action_key}] if action_key else [])
        task_id = self.ledger.create_task(
            issue_number=int(review["issue_number"]),
            pr_number=int(review["pr_number"]),
            task_type="REPAIR",
            agent="CODEX_CHATGPT",
            model_class="CODEX_DEFAULT",
            task_class=str(review["task_class"]),
            branch=None,
            previous_sha=str(review["target_sha"]),
            blockers=payload,
            parent_id=str(review["id"]),
        )
        try:
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
        except Exception as exc:
            self.runtime.event(
                "HANDOFF_MIRROR_FAILED", assignment_id=task_id,
                issue=review["issue_number"], pr=review["pr_number"],
                child_type="REPAIR", error=str(exc),
            )

    def enqueue_replacement_review(self, old: sqlite3.Row, current_sha: str) -> None:
        if self.ledger.child(str(old["id"]), "REVIEW", current_sha):
            return
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
        try:
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
        except Exception as exc:
            self.runtime.event(
                "HANDOFF_MIRROR_FAILED", assignment_id=task_id,
                issue=old["issue_number"], pr=old["pr_number"],
                child_type="REVIEW", target_sha=current_sha, error=str(exc),
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
            # CI failures are repairable code/test defects, not owner blockers.
            blockers = (
                self.gh.failed_check_blockers(target)
                if hasattr(self.gh, "failed_check_blockers") else []
            )
            if not blockers:
                blockers = [{"protocol_version": 1, "class": "TERMINAL", "source_kind": "CI",
                             "source_id": f"{target}:{detail}", "subject_sha": target,
                             "rule_id": "UNCLASSIFIED_CI_FAILURE", "observed": {"detail": detail},
                             "requested_action": {}}]
            self.ledger.update(
                task["id"],
                status="DONE",
                blockers_json=json.dumps(blockers),
                retry_at=None,
                last_error="CI failure queued for autonomous repair",
            )
            self.runtime.event(
                "CI_REPAIR_ENQUEUED",
                assignment_id=task["id"],
                issue=task["issue_number"],
                pr=task["pr_number"],
                target_sha=target,
                detail=detail,
            )
            self.dispatch_remediations(task, blockers, source_kind="CI",
                                       source_id=f"{target}:{detail}")
            return
        files = self.gh.changed_files(int(task["pr_number"]))
        issue = self.gh.issue(int(task["issue_number"]))
        sensitive = any(
            name.startswith(prefix)
            for name in files
            for prefix in self.cfg["safety"]["no_auto_merge_path_prefixes"]
        )
        if sensitive:
            if str(issue.get("author_association") or "") not in self.trusted:
                self.block(task, "protected AI-control-plane change lost trusted issue author")
                return
            protected_files = [
                name
                for name in files
                if any(
                    name.startswith(prefix)
                    for prefix in self.cfg["safety"]["no_auto_merge_path_prefixes"]
                )
            ]
            if not all(name in AUTO_APPLY_CONTROL_PLANE_PATHS for name in protected_files):
                self.block(
                    task,
                    "protected change contains path outside AI control-plane allowlist",
                )
                return
            if not acceptance_flag(str(issue.get("body") or ""), "AI_TEAM_PROTECTED_CHANGE"):
                self.block(
                    task,
                    "protected AI-control-plane change lacks "
                    "AI_TEAM_PROTECTED_CHANGE=YES",
                )
                return
            self.gh.comment(
                int(task["pr_number"]),
                "AI_TEAM_PROTECTED_GATE=PASS\n"
                "Trusted Issue authorization + independent exact-SHA Claude PASS + CI green; "
                + "all protected files are inside the narrow AI control-plane allowlist.",
            )
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
            # Merge/API rejection can be transient; retry this stage only.
            retry_at = retry_at_after(max(60, int(self.cfg["poll_seconds"])))
            self.ledger.update(
                task["id"],
                status="WAITING_CI",
                retry_at=retry_at,
                last_error=f"merge rejected; automatic retry scheduled: {merged}",
            )
            self.runtime.event(
                "MERGE_RETRY_SCHEDULED",
                assignment_id=task["id"],
                issue=task["issue_number"],
                pr=task["pr_number"],
                target_sha=target,
                retry_after=retry_at,
            )
            return
        merged_at = utcnow()
        merged_sha = str(merged.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", merged_sha):
            self.block(task, "merge response lacks exact merged commit SHA")
            return
        self.ledger.record_merged_code(issue_number=int(task["issue_number"]),
                                       code_sha=merged_sha, pr_number=int(task["pr_number"]),
                                       observed_at=merged_at)
        contract = self.completion_contract(issue)
        self.ledger.update(task["id"], status="DONE", retry_at=None, last_error=None,
                           lifecycle_phase="MERGED",
                           completion_contract_json=canonical_json(contract))
        self.gh.remove_label(int(task["pr_number"]), self.cfg["labels"]["waiting_review"])
        self.gh.comment(
            int(task["issue_number"]),
            f"AI_TEAM_AUTONOMOUS_MERGE=YES\nPR={task['pr_number']}\nTARGET_SHA={target}\n"
            f"CI=PASS\nASYNC_CLAUDE_AUDIT=NO\nMERGED_AT={merged_at}\n"
            f"LIFECYCLE=MERGED\nACCEPTANCE_PENDING={'NO' if contract['close_on_merge'] else 'YES'}",
        )
        if contract["close_on_merge"]:
            self.ledger.update(task["id"], lifecycle_phase="PROVEN")
            self.runtime.event("ACCEPTANCE_PROVEN", assignment_id=task["id"],
                               issue=task["issue_number"], target_sha=target,
                               status="PROVEN", lifecycle_phase="PROVEN")
            self.finalize_proven_issue(issue, self.ledger.get(str(task["id"])),
                                       result="explicit close-on-merge contract proven")
            return
        next_task = self.enqueue_acceptance(issue, parent_id=str(task["id"]),
                                            merged_sha=merged_sha)
        if next_task is None:
            self.block(self.ledger.get(str(task["id"])),
                       "post-merge contract has no runnable unmet phase")

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
            systemd_unit=None,
            last_error=error,
        )

    def block(self, task: sqlite3.Row, error: str) -> None:
        self.ledger.update(
            task["id"], status="BLOCKED", retry_at=None,
            last_error=error[:1500], systemd_unit=None,
        )
        number = int(task["issue_number"])
        try:
            self.gh.add_labels(number, [self.cfg["labels"]["blocked"]])
            self.gh.remove_label(number, self.cfg["labels"]["pending"])
            self.gh.remove_label(number, self.cfg["labels"]["ready"])
            self.gh.remove_label(number, self.cfg["labels"]["queued"])
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
