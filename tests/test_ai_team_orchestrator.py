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


def test_routine_review_routes_to_independent_codex():
    assert orch.route_review(orch.DEFAULT_CONFIG, "ROUTINE", None) == "CODEX_DEFAULT"
    assert orch.review_profile(orch.DEFAULT_CONFIG, "ROUTINE") == [
        "DETERMINISTIC_PREFLIGHT", "CODEX_REVIEW", "CI"
    ]


def test_quant_review_keeps_specialists_after_codex():
    assert (
        orch.route_review(
            orch.DEFAULT_CONFIG,
            "QUANT_PROFITABILITY",
            "QUANT_PROFITABILITY",
        )
        == "CODEX_DEFAULT"
    )
    profile = orch.review_profile(orch.DEFAULT_CONFIG, "QUANT_PROFITABILITY")
    assert "SONNET_CHALLENGE" in profile
    assert "OPUS_FINAL" in profile


@pytest.mark.parametrize("task_class", [
    "QUANT_PROFITABILITY", "STATISTICAL_METHODOLOGY",
    "CAPITAL_SENSITIVE_METHODOLOGY", "UNRESOLVED_DISAGREEMENT",
])
def test_high_stakes_profiles_retain_final_opus(task_class):
    assert "OPUS_FINAL" in orch.review_profile(orch.DEFAULT_CONFIG, task_class)


def test_major_architecture_uses_codex_then_sonnet():
    assert orch.route_review(orch.DEFAULT_CONFIG, "MAJOR_ARCHITECTURE", None) == "CODEX_DEFAULT"
    assert "SONNET_CHALLENGE" in orch.review_profile(orch.DEFAULT_CONFIG, "MAJOR_ARCHITECTURE")


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


def test_explicit_review_profile_is_authoritative_and_invalid_fails_closed():
    body = "AI_TASK_CLASS=ROUTINE\nAI_TEAM_REVIEW_PROFILE=ENGINE_CRITICAL"
    assert orch.parse_review_profile(body, orch.DEFAULT_CONFIG, "ROUTINE") == "ENGINE_CRITICAL"
    with pytest.raises(ValueError, match="INVALID_REVIEW_PROFILE"):
        orch.parse_initial_route(
            "AI_TASK_CLASS=ROUTINE\nAI_TEAM_REVIEW_PROFILE=EXPENSIVE_GUESS"
        )


@pytest.mark.parametrize("task_class", ["MAJOR_ARCHITECTURE", "QUANT_PROFITABILITY"])
def test_high_value_initial_route_is_opus_research(task_class):
    route = orch.parse_initial_route(
        f"AI_TASK_CLASS={task_class}\nAI_INITIAL_ROUTE=RESEARCH\n"
        "AI_INITIAL_AGENT=CLAUDE\nAI_INITIAL_MODEL=OPUS"
    )
    assert route == {"task_class": task_class, "task_type": "RESEARCH",
                     "agent": "CLAUDE", "model_class": "OPUS"}


def test_routine_route_and_bad_route_fail_closed():
    assert orch.parse_initial_route("AI_TASK_CLASS=ROUTINE")["agent"] == "CODEX_CHATGPT"
    with pytest.raises(ValueError, match="INVALID_INITIAL_ROUTE"):
        orch.parse_initial_route(
            "AI_TASK_CLASS=MAJOR_ARCHITECTURE\nAI_INITIAL_ROUTE=BUILD\n"
            "AI_INITIAL_AGENT=CODEX_CHATGPT\nAI_INITIAL_MODEL=CODEX_DEFAULT"
        )


def test_legacy_queued_route_migrates_without_weakening_new_entry_validation():
    assert orch.parse_initial_route(
        "AI_TEAM_AUTO_QUEUE=YES\nAI_TEAM_QUEUE_PRIORITY=1"
    ) == {"task_class": "ROUTINE", "task_type": "BUILD",
          "agent": "CODEX_CHATGPT", "model_class": "CODEX_DEFAULT"}
    assert orch.parse_initial_route(
        "AI_TEAM_AUTO_QUEUE=YES\nAI_TASK_CLASS=MAJOR_ARCHITECTURE"
    )["model_class"] == "OPUS"
    with pytest.raises(ValueError, match="missing AI_TASK_CLASS"):
        orch.parse_initial_route("ordinary new issue")


def test_protected_authorization_is_repository_issued_and_exact_sha():
    sha = "a" * 40
    auth = orch.parse_protected_action_authorization(
        "AI_PROTECTED_AUTH_ID=user-1\n"
        "AI_PROTECTED_AUTH_ACTION=DEPLOY_REVIEWED_CONTROL_PLANE\n"
        f"AI_PROTECTED_AUTH_SUBJECT_SHA={sha}\n"
        "AI_PROTECTED_AUTH_EXPIRES_AT=2099-01-01T00:00:00Z\n"
        "AI_PROTECTED_AUTH_MAX_ACTIONS=1"
    )
    assert auth == {"id": "user-1", "action": "DEPLOY_REVIEWED_CONTROL_PLANE",
                    "subject_sha": sha, "expires_at": "2099-01-01T00:00:00Z",
                    "max_actions": 1}
    assert orch.parse_protected_action_authorization(
        "AI_PROTECTED_AUTH_ID=model-only"
    ) is None


def test_remediation_fingerprint_is_idempotent(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    blocker = {"protocol_version": 1, "class": "CODE_CHANGE", "source_kind": "REVIEW",
               "source_id": "review-1", "subject_sha": "a" * 40, "rule_id": "broken",
               "observed": {"paths": ["x.py"], "reproducer": "pytest -q"},
               "requested_action": {"paths": ["x.py"]}}
    first = ledger.observe_remediation(blocker, issue_number=1, pr_number=2,
                                       actor="CODEX_CHATGPT")
    second = ledger.observe_remediation(blocker, issue_number=1, pr_number=2,
                                        actor="CODEX_CHATGPT")
    assert first["fingerprint"] == second["fingerprint"]
    assert second["occurrence_count"] == 2
    assert second["action_attempts"] == 0


def test_preflight_fingerprint_tracks_sha_check_and_failure_detail():
    first = orch.deterministic_failure_blocker(
        sha="a" * 40, command="ruff check x.py", detail="F401 line 1", changed=["x.py"]
    )
    progressed = orch.deterministic_failure_blocker(
        sha="b" * 40, command="ruff check x.py", detail="E501 line 2", changed=["x.py"]
    )
    assert first["source_kind"] == "PREFLIGHT"
    assert first["source_id"].startswith("a" * 40)
    assert first["rule_id"] != progressed["rule_id"]


def test_codex_reviewer_sandbox_is_read_only_and_transcript_free(tmp_path, monkeypatch):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    task_id = ledger.create_task(
        issue_number=195, pr_number=196, task_type="REVIEW", agent="CODEX_REVIEWER",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", target_sha="a" * 40,
    )
    captured = {}
    monkeypatch.setattr(
        orch, "model_sandbox_command", lambda **kw: captured.update(kw) or kw["command"]
    )
    monkeypatch.setattr(
        orch, "run", lambda command, **kw: subprocess.CompletedProcess(command, 0, "", "")
    )
    team = object.__new__(orch.Orchestrator)
    team.cfg = orch.DEFAULT_CONFIG
    team.invoke_codex_review(ledger.get(task_id), tmp_path, "review", "unit")
    assert captured["read_only_worktree"] is True
    assert "resume" not in captured["command"]
    assert captured["home"] == orch.CODEX_REVIEW_HOME
    assert orch.CODEX_REVIEW_HOME != orch.CODEX_HOME
    assert orch.CODEX_REVIEW_WORK != orch.CODEX_WORK


def test_preflight_runs_repo_lint_format_and_all_ai_team_tests(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    for name in ("test_ai_team_one.py", "test_ai_team_two.py"):
        (tmp_path / "tests" / name).write_text("")
    commands = []
    monkeypatch.setattr(
        orch, "run", lambda command, **kw: commands.append(command)
        or subprocess.CompletedProcess(command, 0, "", "")
    )
    assert orch.deterministic_preflight(
        tmp_path, "a" * 40, ["scripts/ai_team_orchestrator.py"]
    ) == []
    assert commands[0][-2:] == ["check", "."]
    assert commands[1][-3:] == ["--check", "."]
    assert commands[2][-2:] == ["tests/test_ai_team_one.py", "tests/test_ai_team_two.py"]


def test_new_sha_invalidates_all_old_review_stages(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    old = "a" * 40
    ids = [ledger.create_task(
        issue_number=1, pr_number=2, task_type=kind, agent="CLAUDE",
        model_class="SONNET", task_class="QUANT", review_profile="QUANT",
        target_sha=old, status=status,
    ) for kind, status in (("REVIEW", "WAITING_CI"), ("CHALLENGE", "WAITING_RATE_LIMIT"))]
    team = object.__new__(orch.Orchestrator)
    team.ledger = ledger
    team.invalidate_reviews_for_other_shas(2, "b" * 40)
    assert {ledger.get(task_id)["status"] for task_id in ids} == {"STALE"}


def test_quant_opus_is_behind_prospective_evidence_state(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    parent_id = ledger.create_task(
        issue_number=1, pr_number=2, task_type="CHALLENGE", agent="CLAUDE",
        model_class="SONNET", task_class="ROUTINE", review_profile="QUANT",
        target_sha="a" * 40, status="DONE",
    )
    team = object.__new__(orch.Orchestrator)
    team.ledger = ledger
    team.enqueue_prospective_evidence(ledger.get(parent_id))
    child = ledger.child(parent_id, "PROSPECTIVE_EVIDENCE", "a" * 40)
    assert child is not None
    assert ledger.db.execute("SELECT 1 FROM tasks WHERE model_class='OPUS'").fetchone() is None


def test_explicit_engine_profile_on_routine_task_is_persisted_for_sonnet(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    parent_id = ledger.create_task(
        issue_number=196, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="DONE",
    )

    class GH:
        def issue(self, number):
            return {"body": "AI_TASK_CLASS=ROUTINE\nAI_TEAM_REVIEW_PROFILE=ENGINE_CRITICAL"}

        def comment(self, *args):
            pass

        def add_labels(self, *args):
            pass

    team = object.__new__(orch.Orchestrator)
    team.cfg, team.ledger, team.gh = orch.DEFAULT_CONFIG, ledger, GH()
    team.runtime = type("Runtime", (), {"event": lambda *args, **kwargs: None})()
    team.enqueue_review(ledger.get(parent_id), 199, "a" * 40)
    review = ledger.child(parent_id, "REVIEW")
    assert review["review_profile"] == "ENGINE_CRITICAL"
    assert "SONNET_CHALLENGE" in orch.task_review_profile(team.cfg, review)


def test_red_ci_parks_assigned_claude_and_routes_exact_sha_to_codex(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    sha = "a" * 40
    task_id = ledger.create_task(
        issue_number=1, pr_number=2, task_type="CHALLENGE", agent="CLAUDE",
        model_class="SONNET", task_class="ROUTINE", review_profile="ENGINE_CRITICAL",
        target_sha=sha,
    )

    class GH:
        def pr(self, number):
            return {"state": "open", "head": {"sha": sha}}

        def check_state(self, target):
            return "FAIL", "ruff failed"

        def failed_check_blockers(self, target):
            return [orch.deterministic_failure_blocker(
                sha=target, command="ruff check .", detail="F401", changed=["x.py"]
            )]

    captured = []
    team = object.__new__(orch.Orchestrator)
    team.cfg, team.ledger, team.gh = orch.DEFAULT_CONFIG, ledger, GH()
    team.dispatch_remediations = lambda task, blockers, **kw: captured.extend(blockers)
    team.handle_review(ledger.get(task_id))
    assert ledger.get(task_id)["status"] == "STALE"
    assert captured[0]["subject_sha"] == sha
    assert captured[0]["requested_action"]["reproducer"] == "ruff check ."


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


def test_review_prompt_is_explicitly_delta_scoped_and_forbids_repo_wide_rereads(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    target = "b" * 40
    task_id = ledger.create_task(
        issue_number=150, pr_number=151, task_type="REVIEW", agent="CLAUDE",
        model_class="SONNET", task_class="ROUTINE", target_sha=target,
    )
    team = object.__new__(orch.Orchestrator)
    prompt = team.review_prompt(
        {"title": "bounded review", "body": "body", "base": {"sha": "a" * 40}},
        ledger.get(task_id), ["scripts/ai_team_orchestrator.py"], [], [],
    )
    assert f"git diff {'a' * 40}..{target} -- <changed files>" in prompt
    assert "only the changed files listed below" in prompt
    assert "Do not reread the whole repository" in prompt
    assert "Never perform or restart a recursive/repository-wide audit" in prompt


def test_claude_invocation_applies_task_specific_turn_budget(tmp_path, monkeypatch):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    task_id = ledger.create_task(
        issue_number=150, task_type="RESEARCH", agent="CLAUDE", model_class="OPUS",
        task_class="MAJOR_ARCHITECTURE",
    )
    captured = {}
    monkeypatch.setattr(orch, "model_sandbox_command", lambda **kw: kw["command"])
    monkeypatch.setattr(
        orch, "run",
        lambda command, **kw: captured.update(command=command, timeout=kw["timeout"])
        or subprocess.CompletedProcess(command, 0, "", ""),
    )
    team = object.__new__(orch.Orchestrator)
    team.cfg = orch.DEFAULT_CONFIG
    team.invoke_claude(ledger.get(task_id), tmp_path, "prompt", "unit")
    budget_at = captured["command"].index("--max-turns")
    assert captured["command"][budget_at + 1] == "16"
    assert captured["timeout"] == orch.DEFAULT_CONFIG["research_timeout_seconds"]


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
    assert "failed_check_blockers" in handle_ci
    assert "self.dispatch_remediations(task, blockers" in handle_ci
    assert "CI failed after review PASS" not in handle_ci
    assert "MERGE_RETRY_SCHEDULED" in handle_ci
    assert "merge rejected; automatic retry scheduled" in handle_ci


def test_successful_merge_durably_completes_before_terminal_projection(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    task_id = ledger.create_task(
        issue_number=159, pr_number=160, target_sha="a" * 40,
        task_type="REVIEW", agent="CLAUDE", model_class="SONNET",
        task_class="ROUTINE", status="WAITING_CI",
    )
    actions = []

    class GH:
        def pr(self, number):
            return {"head": {"sha": "a" * 40}}
        def check_state(self, sha):
            return "PASS", "green"
        def changed_files(self, number):
            return []
        def issue(self, number):
            return {"author_association": "OWNER", "body": ""}
        def merge(self, number, sha):
            actions.append("merge")
            return {"merged": True}
        def remove_label(self, *args):
            actions.append("github")
        def add_labels(self, *args):
            actions.append("github")
        def close_issue(self, *args):
            actions.append("github")
        def comment(self, *args):
            actions.append("github")

    class Runtime:
        def event(self, kind, **payload):
            assert ledger.get(task_id)["status"] == "DONE"
            actions.append((kind, payload))

    team = object.__new__(orch.Orchestrator)
    team.cfg, team.ledger, team.gh = orch.DEFAULT_CONFIG, ledger, GH()
    team.runtime, team.trusted = Runtime(), {"OWNER"}
    team.handle_ci(ledger.get(task_id))
    kind, payload = actions[-1]
    assert kind == "COMPLETED"
    assert actions[-2] == "github"
    assert payload == {
        "assignment_id": task_id, "issue": 159, "pr": 160,
        "target_sha": "a" * 40, "status": "DONE",
        "result": "merged and proven", "next_action": "Done / Proven",
    }


def test_terminal_projection_failure_does_not_change_successful_merge(tmp_path):
    team = object.__new__(orch.Orchestrator)

    class Runtime:
        def event(self, *args, **kwargs):
            raise OSError("outbox unavailable")

    team.runtime = Runtime()
    team.emit_terminal_projection(
        {"id": "review", "issue_number": 159, "pr_number": 160}, "b" * 40
    )


def test_continuity_loops_cover_review_pr_move_limits_and_restart():
    source = MODULE_PATH.read_text()
    assert "self.dispatch_remediations(task, blockers" in source
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


def test_parent_finalizer_metadata_is_explicit_and_unambiguous():
    assert orch.finalizes_parent("P0 title mentions parent #154") is None
    assert orch.finalizes_parent("AI_TEAM_FINALIZES_PARENT=#154") == 154
    assert orch.finalizes_parent("AI_TEAM_FINALIZES_PARENT=0") is None
    assert orch.finalizes_parent(
        "AI_TEAM_FINALIZES_PARENT=154\nAI_TEAM_FINALIZES_PARENT=155"
    ) is None


def test_parent_finalization_requires_canonical_child_success_and_is_idempotent(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    task_id = ledger.create_task(
        issue_number=161, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="DONE",
    )
    labels = orch.DEFAULT_CONFIG["labels"]
    child = {
        "number": 161, "body": "AI_TEAM_FINALIZES_PARENT=154",
        "author_association": "OWNER", "state": "closed",
        "labels": [{"name": labels["done"]}],
    }
    parent = {
        "number": 154, "state": "open",
        "labels": [{"name": labels["blocked"]}, {"name": labels["queued"]}],
    }

    class GH:
        def __init__(self):
            self.closed = []
        def finalizer_issues(self, done_label):
            return [child]
        def issue(self, number):
            return parent
        def add_labels(self, number, values):
            parent["labels"].extend({"name": value} for value in values)
        def remove_label(self, number, label):
            parent["labels"] = [x for x in parent["labels"] if x["name"] != label]
        def close_issue(self, number):
            self.closed.append(number)
            parent["state"] = "closed"

    class Runtime:
        def __init__(self):
            self.events = []
        def event(self, kind, **payload):
            self.events.append((kind, payload))

    team = object.__new__(orch.Orchestrator)
    team.cfg, team.ledger, team.gh = orch.DEFAULT_CONFIG, ledger, GH()
    team.runtime, team.trusted = Runtime(), {"OWNER"}
    team.sync_runtime_checkpoint = lambda: None
    team.kick_trello_reconciliation = lambda: None
    assert team.reconcile_parent_finalizers() is True
    assert team.reconcile_parent_finalizers() is False
    assert team.gh.closed == [154]
    assert [kind for kind, _ in team.runtime.events] == ["PARENT_FINALIZED"]
    assert {x["name"] for x in parent["labels"]} == {labels["done"]}

    ledger.update(task_id, status="BLOCKED", last_error="unresolved failure")
    ledger.meta_set("parent_finalized:161:154", "")
    parent["state"] = "open"
    assert team.reconcile_parent_finalizers() is False


def test_untrusted_or_noncanonical_child_cannot_finalize_parent(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    ledger.create_task(
        issue_number=161, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="DONE",
    )
    labels = orch.DEFAULT_CONFIG["labels"]
    child = {
        "number": 161, "body": "AI_TEAM_FINALIZES_PARENT=154",
        "author_association": "NONE", "state": "closed",
        "labels": [{"name": labels["done"]}],
    }

    class GH:
        def finalizer_issues(self, done_label):
            return [child]

    team = object.__new__(orch.Orchestrator)
    team.cfg, team.ledger, team.gh = orch.DEFAULT_CONFIG, ledger, GH()
    team.trusted = {"OWNER"}
    assert team.reconcile_parent_finalizers() is False


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


def test_dependency_blocked_queue_emits_exact_deduplicated_blockers(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    labels = orch.DEFAULT_CONFIG["labels"]
    issue = {
        "number": 120,
        "body": "AI_TEAM_AUTO_QUEUE=YES\nAI_TEAM_QUEUE_PRIORITY=10\nAI_TEAM_DEPENDS_ON=154,155",
        "author_association": "OWNER", "labels": [{"name": labels["queued"]}],
    }

    class GH:
        def pending_issues(self, label):
            return [issue]
        def issue(self, number):
            return {"number": number, "state": "open", "labels": []}

    class Runtime:
        def __init__(self):
            self.events = []
        def event(self, kind, **payload):
            self.events.append((kind, payload))

    team = object.__new__(orch.Orchestrator)
    team.cfg, team.ledger, team.gh = orch.DEFAULT_CONFIG, ledger, GH()
    team.runtime, team.trusted = Runtime(), {"OWNER"}
    team.sync_runtime_checkpoint = lambda: None
    assert team.promote_queued_issue() is False
    assert team.promote_queued_issue() is False
    assert team.runtime.events == [(
        "QUEUE_DEPENDENCY_BLOCKED",
        {"blockers": {120: [154, 155]}, "status": "IDLE_DEPENDENCY_BLOCKED"},
    )]


@pytest.mark.parametrize("terminal_status", ["BLOCKED", "STALE", "DONE"])
def test_fresh_explicit_queue_entry_ignores_terminal_history(tmp_path, terminal_status):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    ledger.create_task(
        issue_number=120, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status=terminal_status,
    )
    labels = orch.DEFAULT_CONFIG["labels"]
    issue = {
        "number": 120,
        "body": "AI_TEAM_AUTO_QUEUE=YES\nAI_TEAM_QUEUE_PRIORITY=10",
        "author_association": "OWNER",
        "labels": [{"name": labels["queued"]}],
    }

    class GH:
        def pending_issues(self, label):
            return [issue]

        def ready_issues(self, label):
            return [issue]

        def add_labels(self, number, values):
            pass

        def remove_label(self, number, label):
            pass

        def comment(self, number, body):
            pass

    class Runtime:
        def event(self, *args, **kwargs):
            pass

    team = object.__new__(orch.Orchestrator)
    team.cfg, team.ledger, team.gh = orch.DEFAULT_CONFIG, ledger, GH()
    team.runtime, team.trusted = Runtime(), {"OWNER"}
    team.sync_runtime_checkpoint = lambda: None
    assert team.promote_queued_issue() is True
    assert ledger.active_for_issue(120) is True


@pytest.mark.parametrize("ineligible_label", ["blocked", "done"])
def test_terminal_issue_label_prevents_queued_issue_promotion(
    tmp_path, ineligible_label
):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    labels = orch.DEFAULT_CONFIG["labels"]
    issue = {
        "number": 120,
        "body": "AI_TEAM_AUTO_QUEUE=YES\nAI_TEAM_QUEUE_PRIORITY=10",
        "author_association": "OWNER",
        "labels": [{"name": labels["queued"]}, {"name": labels[ineligible_label]}],
    }

    class GH:
        def pending_issues(self, label):
            return [issue]

    team = object.__new__(orch.Orchestrator)
    team.cfg, team.ledger, team.gh = orch.DEFAULT_CONFIG, ledger, GH()
    team.trusted = {"OWNER"}
    assert team.promote_queued_issue() is False


def test_active_task_prevents_duplicate_queue_claim(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    ledger.create_task(
        issue_number=120, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE",
    )
    team = object.__new__(orch.Orchestrator)
    team.ledger = ledger
    team.gh = None
    assert team.promote_queued_issue() is False


def test_future_claude_rate_limit_releases_unrelated_codex_queue(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    ledger.create_task(
        issue_number=119, task_type="REVIEW", agent="CLAUDE",
        model_class="SONNET", task_class="ROUTINE", status="WAITING_RATE_LIMIT",
        retry_at="2099-01-01T00:00:00Z",
    )
    assert ledger.has_active_work() is True
    assert ledger.has_queue_claim_conflict() is False


def test_newly_blocked_task_clears_queue_state_and_is_not_reclaimed(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    task_id = ledger.create_task(
        issue_number=120, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE",
    )
    labels = orch.DEFAULT_CONFIG["labels"]
    issue = {
        "number": 120,
        "body": "AI_TEAM_AUTO_QUEUE=YES\nAI_TEAM_QUEUE_PRIORITY=10",
        "author_association": "OWNER",
        "labels": [
            {"name": labels["queued"]}, {"name": labels["ready"]},
            {"name": labels["pending"]},
        ],
    }

    class GH:
        def pending_issues(self, label):
            return [issue] if any(x["name"] == label for x in issue["labels"]) else []

        def add_labels(self, number, values):
            issue["labels"].extend({"name": value} for value in values)

        def remove_label(self, number, label):
            issue["labels"] = [x for x in issue["labels"] if x["name"] != label]

        def comment(self, number, body):
            pass

    class Runtime:
        def event(self, *args, **kwargs):
            pass

    team = object.__new__(orch.Orchestrator)
    team.cfg, team.ledger, team.gh = orch.DEFAULT_CONFIG, ledger, GH()
    team.runtime, team.trusted = Runtime(), {"OWNER"}
    team.sync_runtime_checkpoint = lambda: None
    team.block(ledger.get(task_id), "terminal failure")
    assert {x["name"] for x in issue["labels"]} == {labels["blocked"]}
    assert team.promote_queued_issue() is False
