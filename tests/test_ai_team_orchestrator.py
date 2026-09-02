from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ai_team_orchestrator.py"
spec = importlib.util.spec_from_file_location("ai_team_orchestrator", MODULE_PATH)
assert spec and spec.loader
orch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orch)


def test_routine_review_routes_to_sonnet():
    assert orch.route_review(orch.DEFAULT_CONFIG, "ROUTINE", None) == "SONNET"


def test_quant_review_routes_to_opus_only_under_explicit_class():
    assert (
        orch.route_review(
            orch.DEFAULT_CONFIG,
            "QUANT_PROFITABILITY",
            "QUANT_PROFITABILITY",
        )
        == "OPUS"
    )


def test_opus_escalation_is_rejected_for_routine_work():
    with pytest.raises(RuntimeError, match="non-Opus"):
        orch.route_review(orch.DEFAULT_CONFIG, "ROUTINE", "MAJOR_ARCHITECTURE")


def test_unknown_opus_reason_fails_closed():
    with pytest.raises(RuntimeError, match="invalid Opus"):
        orch.route_review(orch.DEFAULT_CONFIG, "MAJOR_ARCHITECTURE", "BECAUSE_I_WANT_IT")


def test_task_class_fails_closed_and_parses_explicit_class():
    assert orch.parse_task_class("ordinary issue") == ("UNCLASSIFIED", None)
    assert orch.parse_task_class("TASK_CLASS=NOT_A_CLASS") == ("UNCLASSIFIED", None)
    assert orch.parse_task_class("TASK_CLASS=ROUTINE") == ("ROUTINE", None)
    assert orch.parse_task_class(
        "AI_TASK_CLASS=STATISTICAL_METHODOLOGY\nOPUS_ESCALATION_REASON=STATISTICAL_METHODOLOGY\n"
    ) == ("STATISTICAL_METHODOLOGY", "STATISTICAL_METHODOLOGY")


def test_machine_assignment_contains_exact_sha_and_model():
    sha = "a" * 40
    text = orch.assignment_marker(
        task_id="abc123",
        agent="CLAUDE",
        task_type="REVIEW",
        model_class="SONNET",
        task_class="ROUTINE",
        issue_number=10,
        pr_number=11,
        target_sha=sha,
    )
    assert "AI_TEAM_ASSIGNMENT_V1" in text
    assert f"TARGET_SHA={sha}" in text
    assert "MODEL_CLASS=SONNET" in text
    assert "STATUS=PENDING" in text


def test_review_parser_requires_exact_sha():
    sha = "b" * 40
    result = f"REVIEWED_SHA={sha}\nVERDICT=PASS\nBLOCKERS_JSON=[]\n"
    verdict, blockers, _ = orch.extract_review(result, sha)
    assert verdict == "PASS"
    assert blockers == []
    with pytest.raises(RuntimeError, match="stale reviewer SHA"):
        orch.extract_review(result, "c" * 40)


def test_review_fail_preserves_machine_blockers():
    sha = "d" * 40
    result = f'REVIEWED_SHA={sha}\nVERDICT=FAIL\nBLOCKERS_JSON=["test is missing","stale state"]\n'
    verdict, blockers, _ = orch.extract_review(result, sha)
    assert verdict == "FAIL"
    assert blockers == ["test is missing", "stale state"]


def test_rate_limit_persists_future_retry():
    limited, retry_at = orch.rate_limit_info("usage limit reached, try again in 12 minutes", 3600)
    assert limited is True
    assert retry_at is not None
    assert orch.parse_utc(retry_at) is not None


def test_non_rate_failure_does_not_invent_retry_timestamp():
    limited, retry_at = orch.rate_limit_info("ordinary test failure", 3600)
    assert limited is False
    assert retry_at is None


@pytest.mark.parametrize(
    "message",
    [
        "rate limit",
        "usage limit",
        "quota exceeded",
        "too many requests",
        "limit reached",
        "you've hit your limit",
        "weighted-token exhaustion: usage denied",
        "HTTP 429",
        "status code 429",
    ],
)
def test_claude_unavailability_detection_fixtures(message):
    limited, retry_at = orch.rate_limit_info(message, 300)
    assert limited is True
    assert orch.parse_utc(retry_at) is not None


@pytest.mark.parametrize("message", ["resets at 3am", "resets 3 AM", "resets in 47 minutes"])
def test_rate_limit_reset_time_fixtures(message):
    limited, retry_at = orch.rate_limit_info(f"usage limit; {message}", 300)
    assert limited is True
    assert orch.parse_utc(retry_at) > dt.datetime.now(dt.UTC)


def test_rate_limit_iso_reset_fixture():
    limited, retry_at = orch.rate_limit_info(
        "quota exceeded; resets at 2099-02-03T04:05:06Z", 300
    )
    assert limited is True
    assert retry_at == "2099-02-03T04:05:06Z"


def test_unknown_rc_one_text_remains_ordinary_failure():
    assert orch.rate_limit_info("process exited rc=1: assertion failed", 300) == (False, None)


def test_review_enqueue_never_creates_pre_review_merge_gate(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    parent_id = ledger.create_task(
        issue_number=151, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="DONE",
    )

    class GitHubStub:
        def issue(self, number):
            return {"body": "AI_TEAM_PROTECTED_CHANGE=YES\nAI_TEAM_ROUTINE_ASYNC_REVIEW=YES"}

        def comment(self, *args):
            return None

        def add_labels(self, *args):
            return None

    class RuntimeStub:
        def event(self, *args, **kwargs):
            return None

    team = object.__new__(orch.Orchestrator)
    team.cfg = orch.DEFAULT_CONFIG
    team.ledger = ledger
    team.gh = GitHubStub()
    team.runtime = RuntimeStub()
    team.enqueue_review(ledger.get(parent_id), 152, "a" * 40)
    rows = ledger.db.execute("SELECT * FROM tasks WHERE parent_id=?", (parent_id,)).fetchall()
    audit = next(row for row in rows if row["task_type"] == "REVIEW")
    assert audit["status"] == "PENDING"
    assert audit["target_sha"] == "a" * 40
    assert ledger.db.execute(
        "SELECT * FROM tasks WHERE task_type='ASYNC_MERGE'"
    ).fetchone() is None


def test_ledger_recovers_orchestrator_restart_mid_task(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    task_id = ledger.create_task(
        issue_number=1,
        task_type="BUILD",
        agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT",
        task_class="ROUTINE",
        status="RUNNING",
    )
    ledger.recover_interrupted()
    row = ledger.get(task_id)
    assert row["status"] == "RETRY"
    assert "restarted" in row["last_error"]


def test_ledger_keeps_rate_limited_task_and_resume_session(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    task_id = ledger.create_task(
        issue_number=2,
        task_type="REVIEW",
        agent="CLAUDE",
        model_class="SONNET",
        task_class="ROUTINE",
        status="WAITING_RATE_LIMIT",
        retry_at="2999-01-01T00:00:00Z",
        session_id="session-keep-me",
    )
    row = ledger.get(task_id)
    assert row["session_id"] == "session-keep-me"
    assert ledger.due() is None


def test_scheduler_skips_not_due_claude_wait_for_codex(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    ledger.create_task(
        issue_number=2, task_type="REVIEW", agent="CLAUDE", model_class="SONNET",
        task_class="ROUTINE", status="WAITING_RATE_LIMIT",
        retry_at="2999-01-01T00:00:00Z", session_id="same-session",
    )
    codex_id = ledger.create_task(
        issue_number=3, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE",
    )
    assert ledger.due()["id"] == codex_id


def test_probe_wait_and_success_preserve_attempt_and_session(tmp_path, monkeypatch):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    task_id = ledger.create_task(
        issue_number=2, pr_number=12, task_type="REVIEW", agent="CLAUDE",
        model_class="SONNET", task_class="ROUTINE", status="WAITING_RATE_LIMIT",
        retry_at=orch.utcnow(), session_id="same-session", attempt=2, target_sha="a" * 40,
    )
    team = object.__new__(orch.Orchestrator)
    team.ledger = ledger
    team.cfg = {**orch.DEFAULT_CONFIG, "claude_readiness_probe_seconds": 300,
                "claude_readiness_probe_timeout_seconds": 20,
                "claude_readiness_probe_output_bytes": 4096}

    class Events:
        def event(self, *args, **kwargs):
            return None

    team.runtime = Events()
    monkeypatch.setattr(orch, "model_sandbox_command", lambda **kw: kw["command"])
    replies = iter([
        subprocess.CompletedProcess([], 1, "", "usage limit; resets in 47 minutes"),
        subprocess.CompletedProcess([], 0, '{"result":"CLAUDE_READY_OK"}', ""),
    ])
    monkeypatch.setattr(orch, "run", lambda *args, **kwargs: next(replies))
    team.handle_claude_probe(ledger.get(task_id))
    waiting = ledger.get(task_id)
    assert waiting["status"] == "WAITING_RATE_LIMIT"
    assert waiting["attempt"] == 2
    assert waiting["session_id"] == "same-session"
    team.handle_claude_probe(waiting)
    ready = ledger.get(task_id)
    assert ready["status"] == "PENDING"
    assert ready["attempt"] == 2
    assert ready["session_id"] == "same-session"


def test_non_limit_probe_failure_consumes_budget_and_leaves_wait_state(tmp_path, monkeypatch):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    task_id = ledger.create_task(
        issue_number=2, pr_number=12, task_type="REVIEW", agent="CLAUDE",
        model_class="SONNET", task_class="ROUTINE", status="WAITING_RATE_LIMIT",
        retry_at=orch.utcnow(), session_id="same-session", attempt=0,
        target_sha="a" * 40,
    )
    team = object.__new__(orch.Orchestrator)
    team.ledger = ledger
    team.cfg = {
        **orch.DEFAULT_CONFIG,
        "max_attempts": 3,
        "claude_readiness_probe_seconds": 300,
        "claude_readiness_probe_timeout_seconds": 20,
        "claude_readiness_probe_output_bytes": 4096,
    }

    class Events:
        def event(self, *args, **kwargs):
            return None

    team.runtime = Events()
    monkeypatch.setattr(orch, "model_sandbox_command", lambda **kw: kw["command"])
    monkeypatch.setattr(
        orch, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 1, "", "authentication configuration failed"
        ),
    )
    team.handle_claude_probe(ledger.get(task_id))
    row = ledger.get(task_id)
    assert row["status"] == "RETRY"
    assert row["attempt"] == 1
    assert row["session_id"] == "same-session"
    assert "ordinary failure" in row["last_error"]
    assert row["limit_text"] is None

def test_watchdog_requeues_same_review_checkpoint(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    task_id = ledger.create_task(
        issue_number=4, pr_number=14, task_type="REVIEW", agent="CLAUDE",
        model_class="SONNET", task_class="ROUTINE", status="RUNNING",
        target_sha="b" * 40, session_id="checkpoint-session", attempt=1,
        systemd_unit="hl-ai-claude-deadbeef-1",
    )
    stale = ledger.recover_interrupted()
    assert stale[0]["id"] == task_id
    row = ledger.get(task_id)
    assert row["status"] == "RETRY"
    assert row["target_sha"] == "b" * 40
    assert row["session_id"] == "checkpoint-session"


def test_only_one_active_task_per_issue_is_detected(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    ledger.create_task(
        issue_number=3,
        task_type="BUILD",
        agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT",
        task_class="ROUTINE",
    )
    assert ledger.active_for_issue(3) is True
    assert ledger.active_for_issue(4) is False


def test_sandbox_hides_root_home_and_entire_data_mount():
    command = orch.model_sandbox_command(
        unit="test-unit",
        user=orch.CODEX_USER,
        home=orch.CODEX_HOME,
        workdir=orch.CODEX_WORK / "test",
        command=["/usr/local/bin/codex", "--version"],
    )
    joined = " ".join(command)
    assert "ProtectHome=yes" in joined
    assert "InaccessiblePaths=/mnt" in joined
    assert "NoNewPrivileges=yes" in joined


def test_forbidden_live_enablement_patterns_are_present():
    patterns = orch.DEFAULT_CONFIG["safety"]["forbidden_enable_patterns"]
    text = "REAL_TRADING_ENABLED=YES"
    import re

    assert any(re.search(p, text, flags=re.I) for p in patterns)


def test_live_sensitive_path_is_not_auto_mergeable():
    protected = orch.DEFAULT_CONFIG["safety"]["no_auto_merge_path_prefixes"]
    assert any("src/hlcopy/trading/permissions.py".startswith(p) for p in protected)


@pytest.mark.parametrize(
    "path",
    [
        "deploy/systemd/hyperliquid-ai-team-orchestrator.service",
        ".github/workflows/deploy-ai-team-orchestrator.yml",
        "config/ai_team_router.json",
        "scripts/ai_team_orchestrator.py",
        "scripts/ai_team_runtime_ledger.py",
        "scripts/install_codex_code_mode_host.sh",
        "scripts/install_ai_team_orchestrator.sh",
    ],
)
def test_orchestrator_control_plane_paths_are_not_auto_mergeable(path):
    protected = orch.DEFAULT_CONFIG["safety"]["no_auto_merge_path_prefixes"]
    assert any(path.startswith(prefix) for prefix in protected)


def test_protected_control_plane_allowlist_excludes_live_and_deploy_paths():
    allowed = orch.AUTO_APPLY_CONTROL_PLANE_PATHS
    assert "scripts/ai_team_orchestrator.py" in allowed
    assert "scripts/ai_team_runtime_ledger.py" in allowed
    assert "config/ai_team_router.json" in allowed
    assert "src/hlcopy/trading/permissions.py" not in allowed
    assert "docs/ai-team/LIVE_TRADING_GATE.md" not in allowed
    assert ".github/workflows/deploy-ai-team-orchestrator.yml" not in allowed


def test_only_explicit_routine_class_is_auto_merge_eligible():
    auto_merge = orch.DEFAULT_CONFIG["auto_merge_task_classes"]
    assert orch.parse_task_class("TASK_CLASS=ROUTINE")[0] in auto_merge
    assert orch.parse_task_class("missing classification")[0] not in auto_merge
    assert orch.parse_task_class("TASK_CLASS=INVALID")[0] not in auto_merge


def test_result_marker_is_machine_readable_json_blockers():
    text = orch.result_marker(
        task_id="r1",
        reviewed_sha="e" * 40,
        verdict="FAIL",
        reviewer="CLAUDE",
        model_class="SONNET",
        blockers=["one"],
        summary="bad thing",
    )
    line = next(x for x in text.splitlines() if x.startswith("BLOCKERS_JSON="))
    assert json.loads(line.split("=", 1)[1]) == ["one"]


def test_root_git_trust_is_scoped_to_exact_worktree(tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["check"] = kwargs.get("check")
        captured["env"] = kwargs.get("env")
        return None

    monkeypatch.setattr(orch, "run", fake_run)
    orch.git_worktree(tmp_path, "rev-parse", "HEAD", check=True)
    assert captured["cmd"][:3] == ["git", "-c", f"safe.directory={tmp_path}"]
    assert captured["cmd"][3:] == ["-C", str(tmp_path), "rev-parse", "HEAD"]
    assert captured["check"] is True
    assert captured["env"]["GIT_OPTIONAL_LOCKS"] == "0"


def test_codex_runtime_preflight_requires_companion_host(tmp_path):
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)
    with pytest.raises(RuntimeError, match="Code Mode host missing"):
        orch.codex_runtime_preflight(codex)

    host = tmp_path / "codex-code-mode-host"
    host.write_text("#!/bin/sh\n")
    host.chmod(0o755)
    bwrap = tmp_path / "bwrap"
    bwrap.write_text("#!/bin/sh\n")
    bwrap.chmod(0o755)
    assert orch.codex_runtime_preflight(codex, bwrap) == host


def _init_git_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    (path / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)
    return subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()


def test_changed_files_includes_new_untracked_file(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    new_file = tmp_path / "docs" / "new.md"
    new_file.parent.mkdir()
    new_file.write_text("harmless\n")
    assert orch.changed_files(tmp_path, base_sha) == ["docs/new.md"]


def test_untracked_file_contents_are_scanned_for_live_enablement(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    new_file = tmp_path / "docs" / "new.md"
    new_file.parent.mkdir()
    new_file.write_text("REAL_TRADING_ENABLED=YES\n")
    with pytest.raises(RuntimeError, match="forbidden live-trading enablement"):
        orch.validate_changes(orch.DEFAULT_CONFIG, tmp_path, base_sha)


def test_commit_and_push_restores_agent_ownership_before_staging(
    tmp_path: Path, monkeypatch
) -> None:
    normalized = []

    def fake_normalize(workdir, user):
        normalized.append((workdir, user))
        return 111, 222

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(orch, "normalize_worktree_ownership", fake_normalize)
    monkeypatch.setattr(orch, "run", fake_run)
    team = object.__new__(orch.Orchestrator)
    team.commit_and_push(
        tmp_path, {"issue_number": 1, "task_type": "BUILD"}, "codex/test"
    )
    assert normalized == [(tmp_path, orch.CODEX_USER)]


def test_codex_resume_places_exec_options_before_resume_subcommand(
    tmp_path: Path, monkeypatch
) -> None:
    seen = {}
    team = object.__new__(orch.Orchestrator)
    team.cfg = {"build_timeout_seconds": 30}

    def fake_sandbox(*, unit, user, home, workdir, command):
        seen["command"] = command
        return command

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(orch, "model_sandbox_command", fake_sandbox)
    monkeypatch.setattr(orch, "run", fake_run)
    team.invoke_codex(
        {"session_id": "session-123"}, tmp_path, "continue", "unit-test"
    )
    assert seen["command"] == [
        "/usr/local/bin/codex",
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "resume",
        "session-123",
        "-",
    ]



def test_recoverable_automation_paths_do_not_terminally_block():
    source = MODULE_PATH.read_text()
    handle_ci = source[source.index("    def handle_ci("):source.index("    def retry_or_block(")]
    assert "CI_REPAIR_ENQUEUED" in handle_ci
    assert "self.enqueue_repair(task, blockers)" in handle_ci
    assert "CI failed after review PASS" not in handle_ci
    assert "MERGE_RETRY_SCHEDULED" in handle_ci
    assert "merge rejected; automatic retry scheduled" in handle_ci


def test_continuity_loops_cover_review_pr_move_limits_and_restart():
    source = MODULE_PATH.read_text()
    assert "self.enqueue_repair(task, blockers)" in source
    assert "self.enqueue_replacement_review(task, current_sha)" in source
    assert "WAITING_RATE_LIMIT" in source
    assert "STALE_RUN_REQUEUED" in source



def test_codex_postprocess_retries_recoverable_failures():
    source = MODULE_PATH.read_text()
    assert "CODEX_POSTPROCESS_RETRY_SCHEDULED" in source
    assert "Codex postprocess/finalize failed" in source
    assert "fail_closed_markers" in source
    assert "owner-sensitive live path" in source
    assert "forbidden live-trading enablement" in source



def test_handoffs_are_idempotent_and_recoverable():
    source = MODULE_PATH.read_text()
    assert "def reconcile_handoffs" in source
    assert "def handoff_candidates" in source
    assert "def child(" in source
    assert "HANDOFF_RECOVERED" in source
    assert "HANDOFF_RECOVERY_RETRY" in source
    assert "HANDOFF_MIRROR_FAILED" in source
    assert 'self.ledger.child(str(parent["id"]), "REVIEW")' in source
    assert 'self.ledger.child(str(review["id"]), "REPAIR")' in source
    assert 'self.ledger.child(str(old["id"]), "REVIEW", current_sha)' in source


def test_codex_limit_and_worker_state_do_not_consume_or_leak():
    source = MODULE_PATH.read_text()
    codex = source[source.index("    def handle_codex("):source.index("    def invoke_codex(")]
    assert "CODEX_WAITING_RATE_LIMIT" in codex
    assert 'attempt=max(0, int(task["attempt"]) - 1)' in codex
    assert 'self.ledger.update(task["id"], systemd_unit=unit)' in codex
    assert "limit_text=limit_text" in codex
    assert "systemd_unit=None" in codex


def test_terminal_block_releases_worker_marker():
    source = MODULE_PATH.read_text()
    start = source.index("    def block(")
    block = source[start:start + 2500]
    assert 'status="BLOCKED"' in block
    assert "systemd_unit=None" in block


def test_queue_metadata_is_explicit_and_strict():
    assert orch.queue_metadata("AI_TEAM_QUEUE_PRIORITY=1") is None
    assert orch.queue_metadata(
        "AI_TEAM_AUTO_QUEUE=YES\nAI_TEAM_QUEUE_PRIORITY=20\nAI_TEAM_DEPENDS_ON=#120, 154"
    ) == (20, (120, 154))
    assert orch.queue_metadata(
        "AI_TEAM_AUTO_QUEUE=YES\nAI_TEAM_QUEUE_PRIORITY=20\nAI_TEAM_DEPENDS_ON=title"
    ) is None


def test_queue_promotes_smallest_satisfied_priority_and_claims_once(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    labels = orch.DEFAULT_CONFIG["labels"]
    issues = [
        {
            "number": 120,
            "body": (
                "AI_TEAM_AUTO_QUEUE=YES\nAI_TEAM_QUEUE_PRIORITY=10\n"
                "AI_TEAM_DEPENDS_ON=154"
            ),
            "author_association": "OWNER",
            "labels": [{"name": labels["queued"]}],
        },
        {
            "number": 150,
            "body": "AI_TEAM_AUTO_QUEUE=YES\nAI_TEAM_QUEUE_PRIORITY=20",
            "author_association": "OWNER",
            "labels": [{"name": labels["queued"]}],
        },
        {
            "number": 151,
            "body": "AI_TEAM_QUEUE_PRIORITY=1",
            "author_association": "OWNER",
            "labels": [{"name": labels["queued"]}],
        },
    ]

    class GH:
        def __init__(self):
            self.ready = []
            self.comments = []
        def pending_issues(self, label):
            return issues
        def ready_issues(self, label):
            return [x for x in issues if x["number"] in self.ready]
        def issue(self, number):
            state = "closed" if number == 154 else "open"
            return {"number": number, "state": state, "labels": []}
        def add_labels(self, number, values):
            if labels["ready"] in values:
                self.ready.append(number)
        def remove_label(self, number, label):
            pass
        def comment(self, number, body):
            self.comments.append(number)

    class Runtime:
        def event(self, *args, **kwargs):
            pass

    team = object.__new__(orch.Orchestrator)
    team.cfg = orch.DEFAULT_CONFIG
    team.ledger, team.gh, team.runtime = ledger, GH(), Runtime()
    team.trusted = {"OWNER"}
    assert team.promote_queued_issue() is True
    assert team.gh.comments == [120]
    assert ledger.active_for_issue(120)
    assert team.promote_queued_issue() is False
    assert team.gh.comments == [120]


def test_block_labels_issue_and_clears_queue_state():
    source = MODULE_PATH.read_text()
    start = source.index("    def block(")
    block = source[start:start + 2500]
    assert 'number = int(task["issue_number"])' in block
    assert 'self.cfg["labels"]["queued"]' in block
    assert 'self.cfg["labels"]["pending"]' in block
