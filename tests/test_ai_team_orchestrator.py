from __future__ import annotations

import datetime as dt
import hashlib
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


def trusted_artifact(tmp_path, *, issue, requirement, phase, sha, value=True,
                     observed_at=None, producer=None):
    expected_producer, predicate, checks = orch.PHASE_EVIDENCE_SCHEMAS[phase]
    producer = producer or expected_producer
    observed_at = observed_at or orch.utcnow()
    root = tmp_path / "evidence"
    input_path = root / producer / f"{issue}-{phase}.input"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"trusted phase input")
    artifact = {
        "artifacts": [{"path": str(input_path),
                       "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest()}],
        "code_sha": sha,
        "issue_number": issue,
        "observed_at": observed_at,
        "phase": phase,
        "policy_version": orch.ACCEPTANCE_POLICY_VERSION,
        "predicate": predicate,
        "producer": producer,
        "requirement": requirement,
        "result": {check: value for check in checks},
        "schema_version": orch.ACCEPTANCE_ARTIFACT_SCHEMA,
        "window": {"end": observed_at, "start": observed_at},
    }
    path = root / producer / f"{issue}-{phase}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = orch.canonical_json(artifact).encode()
    path.write_bytes(raw)
    return root, {"source": producer, "artifact_path": str(path),
                  "artifact_hash": hashlib.sha256(raw).hexdigest()}


def test_routine_review_routes_to_sonnet():
    assert orch.route_review(orch.DEFAULT_CONFIG, "ROUTINE", None) == "SONNET"


def test_completion_contract_is_explicit_and_fail_closed():
    with pytest.raises(ValueError, match="MISSING_COMPLETION_CONTRACT"):
        orch.parse_completion_contract("acceptance is somewhere in prose")
    assert orch.parse_completion_contract("AI_TEAM_CLOSE_ON_MERGE=YES") == {
        "version": 1, "close_on_merge": True, "requirements": []
    }
    assert orch.parse_completion_contract(
        "AI_TEAM_COMPLETION_REQUIRES=RUNTIME_PROOF,MEASUREMENT_PROOF"
    )["requirements"] == ["RUNTIME_PROOF", "MEASUREMENT_PROOF"]
    with pytest.raises(ValueError, match="CONFLICTS"):
        orch.parse_completion_contract(
            "AI_TEAM_CLOSE_ON_MERGE=YES\nAI_TEAM_COMPLETION_REQUIRES=RUNTIME_PROOF"
        )


def test_completion_contract_missing_number_preserves_fail_closed_error():
    team = object.__new__(orch.Orchestrator)
    team.cfg, team.trusted = orch.DEFAULT_CONFIG, {"OWNER"}

    with pytest.raises(ValueError, match="MISSING_COMPLETION_CONTRACT"):
        team.completion_contract({"author_association": "OWNER", "body": ""})


def test_acceptance_evidence_is_recomputed_from_trusted_artifact(tmp_path):
    root, evidence = trusted_artifact(
        tmp_path, issue=1, requirement="RUNTIME_PROOF",
        phase="PRODUCTION_VALIDATION", sha="a" * 40,
    )
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3", root)
    ledger.create_task(issue_number=1, task_type="PRODUCTION_VALIDATION",
                       agent="CODEX_CHATGPT", model_class="CODEX_DEFAULT",
                       lifecycle_phase="PRODUCTION_VALIDATION", target_sha="a" * 40)
    ledger.record_acceptance_evidence(issue_number=1, requirement="RUNTIME_PROOF",
                                      phase="PRODUCTION_VALIDATION", evidence=evidence,
                                      expected_merged_sha="a" * 40)
    assert ledger.proven_requirements(1) == {"RUNTIME_PROOF"}


@pytest.mark.parametrize("mutation,error", [
    ({"predicate_result": True, "source": "runtime-observer"}, "INCOMPLETE"),
    ({"artifact_hash": "0" * 64}, "HASH_MISMATCH"),
    ({"source": "model-self-attestation"}, "UNTRUSTED_EVIDENCE_SOURCE"),
])
def test_fabricated_envelopes_fake_hashes_and_untrusted_sources_fail_closed(
        tmp_path, mutation, error):
    root, evidence = trusted_artifact(
        tmp_path, issue=2, requirement="RUNTIME_PROOF",
        phase="PRODUCTION_VALIDATION", sha="a" * 40,
    )
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3", root)
    if "predicate_result" in mutation:
        evidence = mutation
    else:
        evidence.update(mutation)
    with pytest.raises(ValueError, match=error):
        ledger.record_acceptance_evidence(
            issue_number=2, requirement="RUNTIME_PROOF", phase="PRODUCTION_VALIDATION",
            evidence=evidence, expected_merged_sha="a" * 40,
        )
    assert ledger.proven_requirements(2) == set()


@pytest.mark.parametrize("bad_sha,stale,error", [
    ("b" * 40, False, "CODE_SHA_MISMATCH"),
    ("a" * 40, True, "STALE_ACCEPTANCE_EVIDENCE"),
])
def test_wrong_merged_sha_and_stale_artifacts_fail_closed(tmp_path, bad_sha, stale, error):
    observed = (
        (
            dt.datetime.now(dt.timezone.utc)  # noqa: UP017 - VM supports Python 3.10
            - dt.timedelta(days=8)
        )
        .isoformat()
        .replace("+00:00", "Z")
        if stale
        else None
    )
    root, evidence = trusted_artifact(
        tmp_path, issue=3, requirement="RUNTIME_PROOF",
        phase="PRODUCTION_VALIDATION", sha=bad_sha, observed_at=observed,
    )
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3", root)
    with pytest.raises(ValueError, match=error):
        ledger.record_acceptance_evidence(
            issue_number=3, requirement="RUNTIME_PROOF", phase="PRODUCTION_VALIDATION",
            evidence=evidence, expected_merged_sha="a" * 40,
        )


def test_fake_or_mutated_referenced_hash_cannot_remain_proven(tmp_path):
    root, evidence = trusted_artifact(
        tmp_path, issue=4, requirement="MEASUREMENT_PROOF",
        phase="MEASUREMENT", sha="a" * 40,
    )
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3", root)
    ledger.create_task(issue_number=4, task_type="MEASUREMENT", agent="CODEX_CHATGPT",
                       model_class="CODEX_DEFAULT", lifecycle_phase="MEASUREMENT",
                       target_sha="a" * 40)
    ledger.record_acceptance_evidence(
        issue_number=4, requirement="MEASUREMENT_PROOF", phase="MEASUREMENT",
        evidence=evidence, expected_merged_sha="a" * 40,
    )
    artifact = json.loads(Path(evidence["artifact_path"]).read_text())
    Path(artifact["artifacts"][0]["path"]).write_bytes(b"fabricated replacement")
    assert not ledger.phase_is_proven(4, "MEASUREMENT_PROOF", "MEASUREMENT")
    assert ledger.proven_requirements(4) == set()


@pytest.mark.parametrize("requirement,phase", [
    (requirement, phase)
    for requirement, phases in orch.COMPLETION_REQUIREMENTS.items()
    for phase in phases
])
def test_canonical_trusted_evidence_satisfies_only_its_phase(tmp_path, requirement, phase):
    sha = "c" * 40
    issue = list(orch.PHASE_EVIDENCE_SCHEMAS).index(phase) + 100
    root, evidence = trusted_artifact(
        tmp_path, issue=issue, requirement=requirement, phase=phase, sha=sha,
    )
    ledger = orch.Ledger(tmp_path / f"{issue}-{requirement}.sqlite3", root)
    ledger.create_task(issue_number=issue, task_type=phase, agent="CODEX_CHATGPT",
                       model_class="CODEX_DEFAULT", lifecycle_phase=phase, target_sha=sha)
    ledger.record_acceptance_evidence(
        issue_number=issue, requirement=requirement, phase=phase, evidence=evidence,
        expected_merged_sha=sha,
    )
    assert ledger.phase_is_proven(issue, requirement, phase)
    other_phases = set(orch.EVIDENCE_TASK_TYPES) - {phase}
    assert all(not ledger.phase_is_proven(issue, requirement, other) for other in other_phases)


def test_failed_measurement_enqueues_repair_not_done(tmp_path):
    root, evidence = trusted_artifact(
        tmp_path, issue=92, requirement="MEASUREMENT_PROOF", phase="MEASUREMENT",
        sha="b" * 40, value=False,
    )
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3", root)
    task_id = ledger.create_task(
        issue_number=92, task_type="MEASUREMENT", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", target_sha="b" * 40,
        lifecycle_phase="MEASUREMENT", evidence={"requirement": "MEASUREMENT_PROOF"},
    )
    issue = {"number": 92, "author_association": "OWNER", "state": "open",
             "body": "AI_TEAM_COMPLETION_REQUIRES=MEASUREMENT_PROOF"}

    class GH:
        def issue(self, number):
            return issue

    class Runtime:
        def event(self, *args, **kwargs):
            pass

    team = object.__new__(orch.Orchestrator)
    team.cfg, team.ledger, team.gh = orch.DEFAULT_CONFIG, ledger, GH()
    team.runtime, team.trusted = Runtime(), {"OWNER"}
    repair = team.complete_acceptance_phase(
        ledger.get(task_id), evidence
    )
    assert repair is not None and repair["task_type"] == "REPAIR"
    assert not ledger.successful_issue(92)


def test_quant_review_routes_to_opus_only_under_explicit_class():
    assert (
        orch.route_review(
            orch.DEFAULT_CONFIG,
            "QUANT_PROFITABILITY",
            "QUANT_PROFITABILITY",
        )
        == "OPUS"
    )


@pytest.mark.parametrize("task_class", [
    "QUANT_PROFITABILITY", "STATISTICAL_METHODOLOGY",
    "CAPITAL_SENSITIVE_METHODOLOGY", "UNRESOLVED_DISAGREEMENT",
])
def test_high_stakes_final_reviews_always_route_to_opus(task_class):
    assert orch.route_review(orch.DEFAULT_CONFIG, task_class, None) == "OPUS"


def test_major_architecture_review_uses_sonnet_unless_explicitly_escalated():
    assert orch.route_review(orch.DEFAULT_CONFIG, "MAJOR_ARCHITECTURE", None) == "SONNET"
    assert orch.route_review(
        orch.DEFAULT_CONFIG, "MAJOR_ARCHITECTURE", "MAJOR_ARCHITECTURE"
    ) == "OPUS"


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
    assert orch.parse_utc(retry_at) > dt.datetime.now(dt.timezone.utc)  # noqa: UP017 - VM supports Python 3.10


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


def test_authorized_router_change_is_proposable_but_remains_protected(
    tmp_path: Path,
) -> None:
    base_sha = _init_git_repo(tmp_path)
    router = tmp_path / "config" / "ai_team_router.json"
    router.parent.mkdir()
    router.write_text('{"claude_readiness_probe_seconds": 300}\n')

    files, no_auto = orch.validate_changes(orch.DEFAULT_CONFIG, tmp_path, base_sha)

    assert files == ["config/ai_team_router.json"]
    assert no_auto is True


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
            return {
                "number": number,
                "author_association": "OWNER",
                "body": "AI_TEAM_CLOSE_ON_MERGE=YES",
            }
        def merge(self, number, sha):
            actions.append("merge")
            return {"merged": True, "sha": "b" * 40}
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
        "target_sha": "b" * 40, "status": "DONE",
        "result": "explicit close-on-merge contract proven",
        "lifecycle_phase": "DONE", "next_action": "Done / Proven",
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


@pytest.mark.parametrize(("task_type", "error", "expected"), [
    ("BUILD", "unsafe changed path .github/workflows/x.yml", "PROTECTED_PATH_ATTEMPT"),
    ("BUILD", "MISSING_COMPLETION_CONTRACT", "MISSING_COMPLETION_CONTRACT"),
    ("REVIEW", "review FAIL", "REVIEW_FAILURE"),
    ("BUILD", "CI check failed: lint", "CI_FAILURE"),
    ("BUILD", "runner process exited", "RUNNER_FAILURE"),
    ("DEPLOY", "service failed preflight", "SERVICE/DEPLOYMENT_FAILURE"),
    ("RESEARCH", "provider rate limit quota", "PROVIDER/RATE_LIMIT_WAIT"),
    ("BUILD", "dependency #10 incomplete", "DEPENDENCY_WAIT"),
    ("RESEARCH", "future prospective evidence window", "EVIDENCE_WINDOW_WAIT"),
    ("TERMINAL", "unexpected internal state", "UNKNOWN_INTERNAL"),
    ("TERMINAL", "OWNER_AUTH_REQUIRED: capital permission", "OWNER_AUTH_REQUIRED"),
])
def test_recovery_failure_classification(task_type, error, expected):
    assert orch.classify_recovery_failure({"task_type": task_type, "last_error": error}) == expected


def test_recovery_creates_deduped_scoped_assignment_and_keeps_queue_free(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    parent_id = ledger.create_task(
        issue_number=93, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="BLOCKED",
        target_sha="a" * 40, attempt=2,
        last_error="unsafe changed path .github/workflows/proof.yml",
    )

    class Runtime:
        def __init__(self):
            self.events = []

        def event(self, kind, **payload):
            self.events.append((kind, payload))

    team = object.__new__(orch.Orchestrator)
    team.cfg, team.ledger, team.runtime = orch.DEFAULT_CONFIG, ledger, Runtime()
    team.reconcile_recovery()
    team.reconcile_recovery()

    parent = ledger.get(parent_id)
    recoveries = ledger.db.execute(
        "SELECT * FROM tasks WHERE parent_id=?", (parent_id,)
    ).fetchall()
    assert parent["status"] == "RECOVERY_PENDING"
    assert parent["failure_class"] == "PROTECTED_PATH_ATTEMPT"
    assert len(recoveries) == 1
    context = json.loads(recoveries[0]["evidence_json"])["recovery_context"]
    assert context["failed_assignment"] == parent_id
    assert context["previous_attempt"] == 2
    assert context["target_sha"] == "a" * 40
    assert ledger.due()["id"] == recoveries[0]["id"]
    assert team.runtime.events[0][1]["unrelated_work_continuing"] is True


def test_failed_recovery_child_advances_bounded_chain_then_dead_letters(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    parent_id = ledger.create_task(
        issue_number=99, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="BLOCKED",
        target_sha="a" * 40, attempt=1,
        last_error="runner process exited unexpectedly",
    )

    class Runtime:
        def __init__(self):
            self.events = []

        def event(self, kind, **payload):
            self.events.append((kind, payload))

    team = object.__new__(orch.Orchestrator)
    team.cfg, team.ledger, team.runtime = orch.DEFAULT_CONFIG, ledger, Runtime()

    failed_id = parent_id
    for expected_attempt in range(1, 4):
        team.reconcile_recovery()
        recovery = ledger.db.execute(
            "SELECT * FROM tasks WHERE parent_id=?", (failed_id,)
        ).fetchone()
        assert recovery is not None
        assert int(recovery["recovery_attempt"]) == expected_attempt
        failed_id = recovery["id"]
        ledger.update(
            failed_id, status="BLOCKED",
            last_error=f"runner worktree cleanup failed attempt {expected_attempt}",
        )

    team.reconcile_recovery()
    assert ledger.get(failed_id)["status"] == "QUARANTINED"
    dead_letters = [
        payload for kind, payload in team.runtime.events
        if kind == "RECOVERY_DEAD_LETTERED"
    ]
    assert len(dead_letters) == 1
    assert dead_letters[0]["assignment_id"] == failed_id
    assert dead_letters[0]["recovery_attempt"] == 3

    team.reconcile_recovery()
    dead_letters = [
        payload for kind, payload in team.runtime.events
        if kind == "RECOVERY_DEAD_LETTERED"
    ]
    assert len(dead_letters) == 1
    assert ledger.db.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE recovery_fingerprint=?",
        (ledger.get(failed_id)["recovery_fingerprint"],),
    ).fetchone()["n"] == 4


def test_recovery_waits_have_exact_time_and_owner_action_is_not_rewritten(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    rate_id = ledger.create_task(
        issue_number=1, task_type="RESEARCH", agent="CLAUDE", model_class="OPUS",
        status="BLOCKED", last_error="provider rate limit quota",
        retry_at="2030-01-01T00:00:00Z",
    )
    owner_id = ledger.create_task(
        issue_number=2, task_type="TERMINAL", agent="MANAGER", model_class="NONE",
        status="BLOCKED", last_error="OWNER_AUTH_REQUIRED: explicit approval",
    )

    class Runtime:
        def event(self, *args, **kwargs):
            pass

    team = object.__new__(orch.Orchestrator)
    team.cfg, team.ledger, team.runtime = orch.DEFAULT_CONFIG, ledger, Runtime()
    team.reconcile_recovery()
    assert ledger.get(rate_id)["status"] == "WAITING_RATE_LIMIT"
    assert ledger.get(rate_id)["retry_at"] == "2030-01-01T00:00:00Z"
    assert ledger.get(owner_id)["status"] == "BLOCKED"
    assert ledger.get(owner_id)["failure_class"] == "OWNER_AUTH_REQUIRED"


def test_runtime_projection_exposes_recovery_state(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    ledger.create_task(
        issue_number=223, task_type="RECOVERY", agent="TRUSTED_MANAGER",
        model_class="NONE", status="RECOVERY_PENDING",
        parent_id="failed-parent",
        failure_class="MISSING_COMPLETION_CONTRACT", recovery_fingerprint="f" * 64,
        next_action="reconcile canonical rollout map",
    )
    runtime = orch.RuntimeLedgerFiles(tmp_path / "runtime", tmp_path / "ledger.sqlite3",
                                      orch.REPO, 130)
    recovery = runtime.project_current()["recovery"]
    assert recovery["blocked_count_by_class"] == {"MISSING_COMPLETION_CONTRACT": 1}
    assert recovery["active_assignment"]["recovery_fingerprint"] == "f" * 64
    assert recovery["active_assignment"]["next_action"] == "reconcile canonical rollout map"


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
        "author_association": "OWNER",
        "body": "AI_TEAM_CLOSE_ON_MERGE=YES",
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


def test_parent_finalization_continues_from_parent_merged_sha(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    parent_sha = "d" * 40
    ledger.create_task(
        issue_number=154, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="DONE",
        lifecycle_phase="IMPLEMENTING", target_sha="c" * 40, pr_number=153,
    )
    ledger.create_task(
        issue_number=154, task_type="REVIEW", agent="CLAUDE",
        model_class="SONNET", task_class="ROUTINE", status="DONE",
        lifecycle_phase="MERGED", target_sha=parent_sha, pr_number=153,
    )
    ledger.create_task(
        issue_number=161, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="DONE",
        lifecycle_phase="PROVEN", target_sha="e" * 40,
    )
    labels = orch.DEFAULT_CONFIG["labels"]
    child = {
        "number": 161, "body": "AI_TEAM_FINALIZES_PARENT=154",
        "author_association": "OWNER", "state": "closed",
        "labels": [{"name": labels["done"]}],
    }
    parent = {
        "number": 154, "state": "open", "author_association": "OWNER",
        "body": "AI_TEAM_COMPLETION_REQUIRES=RUNTIME_PROOF", "labels": [],
    }

    class GH:
        def finalizer_issues(self, done_label):
            return [child]
        def issue(self, number):
            return parent
        def add_labels(self, number, values):
            pass
        def remove_label(self, number, label):
            pass

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
    phase = ledger.phase_task(154, "PRODUCTION_VALIDATION")
    assert phase is not None
    assert phase["target_sha"] == parent_sha
    assert [kind for kind, _ in team.runtime.events] == [
        "POST_MERGE_PHASE_ENQUEUED", "PARENT_ACCEPTANCE_CONTINUED"
    ]


def test_cycle_blocks_only_invalid_acceptance_evidence_task(tmp_path):
    root, evidence = trusted_artifact(
        tmp_path, issue=154, requirement="RUNTIME_PROOF",
        phase="PRODUCTION_VALIDATION", sha="a" * 40,
    )
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3", root)
    task_id = ledger.create_task(
        issue_number=154, task_type="PRODUCTION_VALIDATION", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="PENDING",
        lifecycle_phase="PRODUCTION_VALIDATION", target_sha=None,
        evidence={"requirement": "RUNTIME_PROOF", "result": evidence},
    )
    issue = {
        "number": 154, "state": "open", "author_association": "OWNER",
        "body": "AI_TEAM_COMPLETION_REQUIRES=RUNTIME_PROOF", "labels": [],
    }

    class GH:
        def issue(self, number):
            return issue
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
    team.reap_stale_child = lambda task: None
    team.migrate_legacy_remediation = lambda: None
    team.reconcile_completion_rollout = lambda: None
    team.reconcile_handoffs = lambda: None
    team.reconcile_parent_finalizers = lambda: False
    team.sync_runtime_checkpoint = lambda: None
    team.kick_trello_reconciliation = lambda: None
    # This test isolates acceptance-evidence failure semantics rather than queue admission.
    team.claim_ready_issue = lambda: False
    team.promote_queued_issue = lambda: False

    team.cycle()

    task = ledger.get(task_id)
    assert task["status"] == "STALE"
    assert task["failure_class"] != "OWNER_AUTH_REQUIRED"
    assert "MISSING_EXACT_MERGED_SHA" in task["last_error"]


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
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="RUNNING",
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
    task = ledger.get(task_id)
    assert task["status"] == "STALE"
    assert task["failure_class"] != "OWNER_AUTH_REQUIRED"
    assert {x["name"] for x in issue["labels"]} == {labels["pending"]}
    assert team.promote_queued_issue() is False



def test_p0_277_scheduler_reconciliation_regressions(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    false_owner = ledger.create_task(
        issue_number=91, task_type="WAITING_CI", agent="MANAGER", model_class="NONE",
        task_class="ROUTINE", status="BLOCKED", failure_class="OWNER_AUTH_REQUIRED",
        last_error="protected AI-control-plane change lacks AI_TEAM_PROTECTED_CHANGE=YES",
    )
    assert orch.classify_recovery_failure(ledger.get(false_owner)) == "PROTECTED_PATH_ATTEMPT"
    assert ledger.pending_owner_action() is None

    retry = ledger.create_task(
        issue_number=146, task_type="PRODUCTION_VALIDATION", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="RETRY",
        retry_at=orch.utcnow(), last_error="awaiting deterministic phase runner evidence",
    )
    pending = ledger.create_task(
        issue_number=277, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="PENDING",
    )
    assert ledger.due()["id"] == pending
    assert ledger.has_queue_claim_conflict() is False
    ledger.update(pending, status="RUNNING")
    assert ledger.has_queue_claim_conflict() is True
    ledger.update(pending, status="DONE")
    assert ledger.get(retry)["status"] == "RETRY"


def test_p0_277_due_closed_issue_is_retired_without_execution(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    task_id = ledger.create_task(
        issue_number=146, task_type="PRODUCTION_VALIDATION", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="RETRY",
        retry_at=orch.utcnow(), last_error="awaiting deterministic phase runner evidence",
    )

    class GH:
        def issue(self, number):
            assert number == 146
            return {"number": 146, "state": "closed"}

    class Runtime:
        def event(self, *args, **kwargs):
            pass

    team = object.__new__(orch.Orchestrator)
    team.ledger, team.gh, team.runtime = ledger, GH(), Runtime()
    team.reap_stale_child = lambda task: None
    team.sync_runtime_checkpoint = lambda: True
    team.kick_trello_reconciliation = lambda: None
    team.migrate_legacy_remediation = lambda: None
    team.reconcile_recovery = lambda: None
    team.reconcile_completion_rollout = lambda: None
    team.reconcile_handoffs = lambda: None
    team.reconcile_parent_finalizers = lambda: False
    team.claim_ready_issue = lambda: False
    team.promote_queued_issue = lambda: False
    team.cycle()
    retired = ledger.get(task_id)
    assert retired["status"] == "DONE"
    assert retired["last_error"] == "OBSOLETE_CLOSED_ISSUE"
    assert retired["lifecycle_phase"] == "OBSOLETE"
    assert ledger.due() is None


def test_due_pending_priority_is_durable_and_starvation_safe(tmp_path):
    ledger = orch.Ledger(tmp_path / "priority.sqlite3")
    low = ledger.create_task(
        issue_number=9101, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", queue_priority=10,
    )
    high = ledger.create_task(
        issue_number=9102, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", queue_priority=-100,
    )
    ledger.update(low, created_at="2026-09-10T00:00:00Z")
    ledger.update(high, created_at="2026-09-10T00:00:01Z")
    observed = []
    for i in range(6):
        ledger.create_task(
            issue_number=9200 + i, task_type="BUILD", agent="CODEX_CHATGPT",
            model_class="CODEX_DEFAULT", queue_priority=50,
        )
        due = ledger.due()
        assert due is not None
        observed.append(str(due["id"]))
        ledger.update(str(due["id"]), status="DONE")
    assert observed[0] == high
    assert observed[1] == low


def test_due_pending_same_priority_uses_oldest_admission(tmp_path):
    ledger = orch.Ledger(tmp_path / "age.sqlite3")
    older = ledger.create_task(
        issue_number=9301, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", queue_priority=-20,
    )
    newer = ledger.create_task(
        issue_number=9302, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", queue_priority=-20,
    )
    ledger.update(older, created_at="2026-09-10T00:00:00Z")
    ledger.update(newer, created_at="2026-09-10T00:00:01Z")
    assert ledger.due()["id"] == older


def test_waiting_evidence_window_counts_as_active_work(tmp_path):
    ledger = orch.Ledger(tmp_path / "evidence-active.sqlite3")
    task_id = ledger.create_task(
        issue_number=9801, task_type="POST_MERGE_EVIDENCE",
        agent="CODEX_CHATGPT", model_class="CODEX_DEFAULT",
    )
    ledger.update(
        task_id, status="WAITING_EVIDENCE_WINDOW",
        retry_at=(dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
        .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    assert ledger.active_for_issue(9801)
    assert ledger.has_active_work()


def test_reconcile_closed_issue_tasks_retires_stale_retry_before_due_selection(tmp_path):
    ledger = orch.Ledger(tmp_path / "closed-reconcile.sqlite3")
    closed_id = ledger.create_task(
        issue_number=146, task_type="PRODUCTION_VALIDATION",
        agent="CODEX_CHATGPT", model_class="CODEX_DEFAULT",
        status="RETRY", queue_priority=0,
    )
    open_id = ledger.create_task(
        issue_number=277, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", status="PENDING", queue_priority=-100,
    )
    assert ledger.due()["id"] == open_id

    class GH:
        def issue(self, number):
            return {"number": number, "state": "closed" if number == 146 else "open"}

    class Runtime:
        def __init__(self):
            self.events = []
        def event(self, name, **kwargs):
            self.events.append((name, kwargs))

    team = object.__new__(orch.Orchestrator)
    team.ledger, team.gh, team.runtime = ledger, GH(), Runtime()
    team.reap_stale_child = lambda task: None

    assert team.reconcile_closed_issue_tasks() == 1
    closed = ledger.get(closed_id)
    assert closed["status"] == "DONE"
    assert closed["last_error"] == "OBSOLETE_CLOSED_ISSUE"
    assert closed["lifecycle_phase"] == "OBSOLETE"
    assert ledger.get(open_id)["status"] == "PENDING"
    assert ledger.due()["id"] == open_id
    assert any(name == "OBSOLETE_CLOSED_TASK_RETIRED" for name, _ in team.runtime.events)
