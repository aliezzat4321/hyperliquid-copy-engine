from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ai_team_orchestrator.py"
spec = importlib.util.spec_from_file_location("ai_team_orchestrator_behavior", MODULE_PATH)
assert spec and spec.loader
orch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orch)


class RuntimeStub:
    def event(self, *args, **kwargs):
        return None


class ProtectedGHStub:
    def __init__(self, body: str, association: str = "OWNER") -> None:
        self.body = body
        self.association = association
        self.dispatched: list[tuple[str, str, dict]] = []

    def issue(self, number: int):
        return {"number": number, "body": self.body, "author_association": self.association}

    def dispatch_workflow(self, workflow_id: str, ref: str, inputs: dict):
        self.dispatched.append((workflow_id, ref, inputs))
        return None

    def add_labels(self, *args, **kwargs):
        return None

    def remove_label(self, *args, **kwargs):
        return None

    def comment(self, *args, **kwargs):
        return None


def _parent(ledger: orch.Ledger, sha: str):
    task_id = ledger.create_task(
        issue_number=178,
        pr_number=180,
        task_type="REVIEW",
        agent="CLAUDE",
        model_class="OPUS",
        task_class="MAJOR_ARCHITECTURE",
        status="DONE",
        target_sha=sha,
    )
    return ledger.get(task_id)


def _protected_blocker(sha: str, source_id: str = "review-1") -> dict:
    return {
        "protocol_version": 1,
        "class": "PROTECTED_ACTION",
        "source_kind": "REVIEW",
        "source_id": source_id,
        "subject_sha": sha,
        "rule_id": "PROTECTED_TEST",
        "observed": {"reason": "test"},
        # Deliberately include hostile workflow/ref fields. The manager must ignore
        # these and take workflow/ref only from trusted router config.
        "requested_action": {
            "name": "TEST_ACTION",
            "workflow_id": "model-controlled.yml",
            "ref": "model-controlled-ref",
        },
    }


def _auth_body(sha: str, *, action: str = "TEST_ACTION", max_actions: int = 1) -> str:
    return (
        "AI_PROTECTED_AUTH_ID=user-issued-1\n"
        f"AI_PROTECTED_AUTH_ACTION={action}\n"
        f"AI_PROTECTED_AUTH_SUBJECT_SHA={sha}\n"
        "AI_PROTECTED_AUTH_EXPIRES_AT=2099-01-01T00:00:00Z\n"
        f"AI_PROTECTED_AUTH_MAX_ACTIONS={max_actions}\n"
    )


def _team(tmp_path, body: str, *, association: str = "OWNER"):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    cfg = copy.deepcopy(orch.DEFAULT_CONFIG)
    cfg["remediation"]["protected_actions"] = {
        "TEST_ACTION": {"workflow_id": "trusted-safe.yml", "ref": "main"}
    }
    gh = ProtectedGHStub(body, association)
    team = object.__new__(orch.Orchestrator)
    team.cfg = cfg
    team.ledger = ledger
    team.gh = gh
    team.runtime = RuntimeStub()
    team.trusted = {"OWNER", "MEMBER", "COLLABORATOR"}
    team.sync_runtime_checkpoint = lambda: None
    return team, ledger, gh


def test_protected_action_uses_repository_authorization_and_configured_target(tmp_path):
    sha = "a" * 40
    team, ledger, gh = _team(tmp_path, _auth_body(sha))
    parent = _parent(ledger, sha)

    team.dispatch_remediations(
        parent,
        [_protected_blocker(sha)],
        source_kind="REVIEW",
        source_id="review-1",
    )

    assert len(gh.dispatched) == 1
    workflow_id, ref, inputs = gh.dispatched[0]
    assert workflow_id == "trusted-safe.yml"
    assert ref == "main"
    assert inputs["authorization_id"] == "user-issued-1"
    assert inputs["action_key"]
    remediation = ledger.db.execute(
        "SELECT * FROM remediations WHERE class='PROTECTED_ACTION'"
    ).fetchone()
    assert remediation["status"] == "COMPLETED"
    assert remediation["action_attempts"] == 1


@pytest.mark.parametrize(
    "association,action,auth_sha",
    [
        ("NONE", "TEST_ACTION", "a" * 40),
        ("OWNER", "OTHER_ACTION", "a" * 40),
        ("OWNER", "TEST_ACTION", "b" * 40),
    ],
)
def test_protected_action_fails_closed_on_untrusted_or_mismatched_authorization(
    tmp_path, association, action, auth_sha
):
    subject_sha = "a" * 40
    team, ledger, gh = _team(
        tmp_path,
        _auth_body(auth_sha, action=action),
        association=association,
    )
    parent = _parent(ledger, subject_sha)

    team.dispatch_remediations(
        parent,
        [_protected_blocker(subject_sha)],
        source_kind="REVIEW",
        source_id="review-1",
    )

    assert gh.dispatched == []
    assert ledger.get(parent["id"])["status"] == "BLOCKED"


def test_protected_authorization_max_actions_prevents_second_distinct_dispatch(tmp_path):
    sha = "a" * 40
    team, ledger, gh = _team(tmp_path, _auth_body(sha, max_actions=1))
    parent = _parent(ledger, sha)

    team.dispatch_remediations(
        parent,
        [_protected_blocker(sha, "review-1")],
        source_kind="REVIEW",
        source_id="review-1",
    )
    team.dispatch_remediations(
        parent,
        [_protected_blocker(sha, "review-2")],
        source_kind="REVIEW",
        source_id="review-2",
    )

    assert len(gh.dispatched) == 1
    assert ledger.get(parent["id"])["status"] == "BLOCKED"


def test_production_router_has_no_undispatchable_protected_workflow_mapping():
    cfg = json.loads(
        (Path(__file__).resolve().parents[1] / "config" / "ai_team_router.json").read_text()
    )
    assert cfg["remediation"]["protected_actions"] == {}


def test_deterministic_ci_failure_becomes_code_change_not_terminal(monkeypatch):
    gh = orch.GitHub(orch.REPO)
    monkeypatch.setattr(
        gh,
        "api",
        lambda *args, **kwargs: {
            "check_runs": [
                {
                    "id": 101,
                    "name": "pytest",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ]
        },
    )
    blockers = gh.failed_check_blockers("c" * 40)
    assert len(blockers) == 1
    assert blockers[0]["class"] == "CODE_CHANGE"
    assert blockers[0]["rule_id"] == "DETERMINISTIC_CI_FAILURE"
    assert blockers[0]["requested_action"]["reproducer"] == "pytest"


def test_transient_ci_failure_becomes_ci_retry(monkeypatch):
    gh = orch.GitHub(orch.REPO)
    monkeypatch.setattr(
        gh,
        "api",
        lambda *args, **kwargs: {
            "check_runs": [
                {
                    "id": 202,
                    "name": "runner",
                    "status": "completed",
                    "conclusion": "timed_out",
                }
            ]
        },
    )
    blocker = gh.failed_check_blockers("d" * 40)[0]
    assert blocker["class"] == "CI_RETRY"
    assert blocker["requested_action"]["check_run_id"] == 202


def test_codex_build_prompt_contains_immutable_opus_research_artifact(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    evidence = {
        "research_result_id": "result-123",
        "artifact": "OPUS_ARCHITECTURE: use typed remediation",
    }
    task_id = ledger.create_task(
        issue_number=178,
        task_type="BUILD",
        agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT",
        task_class="MAJOR_ARCHITECTURE",
        blockers=[evidence],
    )
    team = object.__new__(orch.Orchestrator)
    team.cfg = copy.deepcopy(orch.DEFAULT_CONFIG)
    prompt = team.codex_prompt(
        {"number": 178, "title": "implementation", "body": "do it"},
        ledger.get(task_id),
        [],
        [evidence],
    )
    assert "IMMUTABLE OPUS RESEARCH INPUT" in prompt
    assert "result-123" in prompt
    assert "OPUS_ARCHITECTURE: use typed remediation" in prompt
