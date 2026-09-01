from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ai_team_runtime_ledger.py"
SPEC = importlib.util.spec_from_file_location("ai_team_runtime_ledger", MODULE_PATH)
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def make_db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE tasks (
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
        CREATE TABLE runs (
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
        """
    )
    db.execute(
        """
        INSERT INTO tasks(
          id,issue_number,pr_number,task_type,agent,model_class,task_class,status,
          branch,target_sha,previous_sha,blockers_json,workdir,session_id,attempt,
          retry_at,last_error,parent_id,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "task123",
            129,
            None,
            "BUILD",
            "CODEX_CHATGPT",
            "CODEX_DEFAULT",
            "ROUTINE",
            "RUNNING",
            "codex/auto-129-task123",
            None,
            None,
            "[]",
            "/var/lib/hyperliquid-ai-team/agents/codex/worktrees/task123",
            "session-abc",
            1,
            None,
            None,
            None,
            "2026-09-01T10:00:00Z",
            "2026-09-01T10:01:00Z",
        ),
    )
    db.execute(
        """
        INSERT INTO runs(task_id,agent,model_class,started_at,exit_code,session_id,input_tokens)
        VALUES(?,?,?,?,?,?,?)
        """,
        ("task123", "CODEX_CHATGPT", "CODEX_DEFAULT", "2026-09-01T10:01:00Z", 0, "session-abc", 123),
    )
    db.commit()
    db.close()


def task_row(db_path: Path) -> sqlite3.Row:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM tasks WHERE id='task123'").fetchone()
    assert row is not None
    db.close()
    return row


def test_redacts_auth_material() -> None:
    text = "Bearer secret.token.value sk-ant-oat01-abcdef github_pat_ABC123 access_token=hello"
    redacted = runtime.redact(text)
    assert "secret.token.value" not in redacted
    assert "sk-ant-oat01" not in redacted
    assert "github_pat_" not in redacted
    assert "access_token=hello" not in redacted


def test_projection_distinguishes_assignment_from_runtime(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    make_db(db_path)
    files = runtime.RuntimeLedgerFiles(tmp_path / "state", db_path, "owner/repo", 130)
    current = files.project_current()
    assert current["assignment"]["codex"]["assignment_id"] == "task123"
    assert current["runtime"]["codex"]["status"] == "RUNNING"
    assert current["assignment"]["claude"] is None
    assert current["safety"] == {"real_trading": "NO", "polymarket_scope": "DENIED"}
    assert (tmp_path / "state" / "current.json").is_file()
    assert (tmp_path / "state" / "checkpoints" / "codex.json").is_file()


def test_run_artifacts_are_bounded_redacted_and_resumable(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    make_db(db_path)
    files = runtime.RuntimeLedgerFiles(tmp_path / "state", db_path, "owner/repo", 130)
    task = task_row(db_path)
    files.run_started(
        7,
        task,
        prompt="do one task; authorization=super-secret",
        systemd_unit="hl-ai-codex-task123",
    )
    retry = "2026-09-01T10:10:00Z"
    files.run_finished(
        7,
        task,
        stdout="ok Bearer secret-token",
        stderr="oauth_token=bad",
        exit_code=75,
        session_id="session-abc",
        usage={"input_tokens": 22, "output_tokens": 3},
        result="paused",
        error="rate limit",
        status="WAITING_RATE_LIMIT",
        retry_after=retry,
    )
    run_dir = tmp_path / "state" / "runs" / "7"
    assert {p.name for p in run_dir.iterdir()} >= {
        "meta.json",
        "prompt.txt",
        "stdout.log",
        "stderr.log",
        "result.json",
        "summary.md",
    }
    assert "super-secret" not in (run_dir / "prompt.txt").read_text()
    assert "secret-token" not in (run_dir / "stdout.log").read_text()
    assert "oauth_token=bad" not in (run_dir / "stderr.log").read_text()
    checkpoint = json.loads((tmp_path / "state" / "checkpoints" / "codex.json").read_text())
    assert checkpoint["session_id"] == "session-abc"
    assert checkpoint["retry_after"] == retry
    assert checkpoint["status"] == "WAITING_RATE_LIMIT"


def test_handoff_is_chat_independent_and_under_four_kb(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite3"
    make_db(db_path)
    files = runtime.RuntimeLedgerFiles(tmp_path / "state", db_path, "owner/repo", 130)
    files.event("TASK_ASSIGNED", issue=129, agent="CODEX_CHATGPT")
    body = files.handoff(
        main_head="a" * 40,
        active_priorities=[{"issue": 129, "title": "P0 durable runtime ledger"}],
    )
    assert "AI_TEAM_RUNTIME_STATUS_V1" in body
    assert "Do not rely on previous chat history" in body
    assert '"issue":129' in body
    assert len(body.encode("utf-8")) < 4096
    assert (tmp_path / "state" / "handoff.md").read_text() == body
