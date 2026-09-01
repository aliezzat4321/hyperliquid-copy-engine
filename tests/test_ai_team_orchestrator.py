from __future__ import annotations

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


def test_task_class_defaults_to_routine_and_parses_explicit_class():
    assert orch.parse_task_class("ordinary issue") == ("ROUTINE", None)
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
