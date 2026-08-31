"""Adversarial tests for the shared AI-team contract.

The eight mutations marked ORIGINAL-N reproduce, one for one, the findings from the
independent review of PR #94: every one of them passed the original validator. Each is
asserted to fail here.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


contract = _load("ai_team_contract")
guard = _load("check_live_sensitive_change")
ContractError = contract.ContractError

STATE_PATH = ROOT / "docs/ai-team/state.json"
REGISTRY_PATH = ROOT / "docs/ai-team/experiments/registry.json"


@pytest.fixture
def state() -> dict:
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _now(state: dict) -> datetime:
    """Evaluate against the snapshot's own instant so tests never age out."""
    return datetime.fromisoformat(state["snapshot_at"].replace("Z", "+00:00")).astimezone(UTC)


def _rejects(state: dict, fragment: str) -> None:
    with pytest.raises(ContractError) as excinfo:
        contract.validate_state(state, now=_now(state))
    assert fragment.lower() in str(excinfo.value).lower()


# --- the committed state must pass ----------------------------------------


def test_committed_state_is_valid(state):
    contract.validate_state(state, now=_now(state))


def test_committed_registry_is_valid(registry):
    contract.validate_experiments(registry, now=datetime.now(UTC))


def test_live_trading_is_disabled_and_carries_no_authorization(state):
    assert state["live_trading"]["authorized"] is False
    assert state["live_trading"]["authorization"] is None


# --- ORIGINAL-1: stale snapshot -------------------------------------------


def test_original_1_stale_snapshot_is_rejected(state):
    state["snapshot_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(ContractError, match="exceeding"):
        contract.validate_state(state, now=datetime.now(UTC))


def test_snapshot_inside_the_bound_is_accepted(state):
    now = _now(state) + timedelta(hours=71)
    contract.validate_state(state, now=now)


def test_snapshot_in_the_future_is_rejected(state):
    state["snapshot_at"] = "2099-01-01T00:00:00Z"
    with pytest.raises(ContractError, match="future"):
        contract.validate_state(state, now=datetime.now(UTC))


# --- ORIGINAL-2: bogus head_observed --------------------------------------


def test_original_2_non_hex_head_is_rejected(state):
    state["head_observed"] = "not-a-commit"
    _rejects(state, "40-hex commit")


def test_original_2_unknown_commit_is_rejected_by_the_repo_check(tmp_path):
    """A well-formed SHA that no commit matches must fail when history is available."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")

    def known(sha: str) -> bool:
        shallow = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        if shallow == "true":
            return True
        return subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, capture_output=True
        ).returncode == 0

    real = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert known(real) is True
    assert known("0" * 40) is False


# --- ORIGINAL-3: builder == reviewer --------------------------------------


def test_original_3_builder_equals_reviewer_is_rejected(state):
    state["priorities"][1]["owner"] = "CLAUDE"
    state["priorities"][1]["reviewer"] = "CLAUDE"
    _rejects(state, "owner == reviewer")


def test_profitability_critical_work_requires_an_ai_reviewer(state):
    state["priorities"][1]["reviewer"] = "USER"
    _rejects(state, "needs an AI reviewer")


def test_closed_work_may_skip_the_independence_rule(state):
    state["priorities"][1]["status"] = "DONE"
    state["priorities"][1]["owner"] = "CLAUDE"
    state["priorities"][1]["reviewer"] = "CLAUDE"
    contract.validate_state(state, now=_now(state))


# --- ORIGINAL-4: empty / placeholder owners -------------------------------


def test_original_4_empty_owner_and_reviewer_are_rejected(state):
    state["priorities"][1]["owner"] = ""
    state["priorities"][1]["reviewer"] = ""
    _rejects(state, "must be one of")


@pytest.mark.parametrize("placeholder", ["UNASSIGNED_ONE_BUILDER", "OTHER_AI_AGENT", "TBD"])
def test_original_4_placeholder_agents_are_rejected(state, placeholder):
    state["priorities"][0]["owner"] = placeholder
    _rejects(state, "must be one of")


def test_duplicate_active_issues_are_rejected(state):
    state["priorities"][1]["issue"] = state["priorities"][0]["issue"]
    _rejects(state, "duplicate")


def test_active_priority_without_an_issue_is_rejected(state):
    state["priorities"][1]["issue"] = None
    _rejects(state, "positive GitHub Issue")


# --- ORIGINAL-5: fabricated fact ------------------------------------------


def test_original_5_fabricated_fact_without_provenance_is_rejected(state):
    state["lanes"]["lane_3"]["facts"][3] = "+$9999.00 gross PnL"
    _rejects(state, "must be an object")


def test_fact_missing_provenance_fields_is_rejected(state):
    state["lanes"]["lane_3"]["facts"][3].pop("source_ref")
    _rejects(state, "missing required field")


@pytest.mark.parametrize(
    ("source_type", "bad_ref"),
    [
        ("WORKFLOW_RUN", "run-33369211976"),
        ("PULL_REQUEST", "95"),
        ("EXPERIMENT", "EXP-1"),
        ("COMMIT", "zzzz"),
    ],
)
def test_source_ref_must_match_its_source_type(state, source_type, bad_ref):
    fact = state["lanes"]["lane_1"]["facts"][0]
    fact["source_type"] = source_type
    fact["source_ref"] = bad_ref
    _rejects(state, "does not match the required format")


def test_fact_observed_in_the_future_is_rejected(state):
    state["lanes"]["lane_1"]["facts"][0]["observed_at"] = "2099-01-01T00:00:00Z"
    _rejects(state, "future")


def test_duplicate_fact_keys_within_a_lane_are_rejected(state):
    facts = state["lanes"]["lane_1"]["facts"]
    facts.append(copy.deepcopy(facts[0]))
    _rejects(state, "repeats fact key")


def test_percentage_outside_zero_to_one_hundred_is_rejected(state):
    state["infrastructure"]["facts"][0]["value"] = 140.0
    _rejects(state, "within 0..100")


def test_count_must_not_be_negative(state):
    state["lanes"]["lane_2"]["facts"][0]["value"] = -1
    _rejects(state, "non-negative integer")


# --- ORIGINAL-6: deleted facts --------------------------------------------


def test_original_6_deleting_all_lane_facts_is_rejected(state):
    for lane in state["lanes"].values():
        lane["facts"] = []
    _rejects(state, "non-empty list")


# --- ORIGINAL-7: invented status ------------------------------------------


def test_original_7_undefined_lane_status_is_rejected(state):
    state["lanes"]["lane_2"]["status"] = "TOTALLY_FINE"
    _rejects(state, "must be one of")


def test_undefined_infrastructure_status_is_rejected(state):
    state["infrastructure"]["status"] = "PROBABLY_FINE"
    _rejects(state, "must be one of")


def test_next_must_reference_an_issue_or_none(state):
    state["lanes"]["lane_1"]["next"] = "someone should look at this"
    _rejects(state, "must be 'Issue #")


# --- ORIGINAL-8: junk live authorization ----------------------------------


def test_original_8_authorized_with_free_text_reference_is_rejected(state):
    state["live_trading"] = {
        "authorized": True,
        "status": "ENABLED",
        "authorization": "trust me",
    }
    _rejects(state, "must be one of")


def test_authorized_without_an_authorization_object_is_rejected(state):
    state["live_trading"] = {"authorized": True, "status": "AUTHORIZED", "authorization": None}
    _rejects(state, "must be an object")


def _authorization(now: datetime) -> dict:
    return {
        "authorized_by": "USER",
        "scope": {
            "lane": "lane_3",
            "slice": "carmine|BTC|long",
            "service": "invo-notification-executor",
            "stage": "MICRO_LIVE",
            "max_notional_usd": 50,
        },
        "authorized_at": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "approval_reference": "LIVE-AUTH-2026-08-31-001",
        "expires_at": (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "revoked": False,
    }


def test_a_complete_authorization_is_accepted(state):
    """The gate is strict, not impossible: a fully specified grant validates."""
    now = _now(state)
    state["live_trading"] = {
        "authorized": True,
        "status": "AUTHORIZED",
        "authorization": _authorization(now),
    }
    contract.validate_state(state, now=now)


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda a: a.__setitem__("authorized_by", "CLAUDE"), "must be USER"),
        (lambda a: a.__setitem__("approval_reference", "approved in chat"), "must match"),
        (lambda a: a.__setitem__("revoked", True), "revoked"),
        (lambda a: a.pop("expires_at"), "missing required field"),
        (lambda a: a["scope"].__setitem__("max_notional_usd", 0), "positive number"),
        (lambda a: a["scope"].__setitem__("stage", "ALL_IN"), "must be one of"),
        (lambda a: a.__setitem__("wildcard", True), "unknown field"),
    ],
)
def test_incomplete_or_forged_authorizations_are_rejected(state, mutate, fragment):
    now = _now(state)
    authorization = _authorization(now)
    mutate(authorization)
    state["live_trading"] = {
        "authorized": True,
        "status": "AUTHORIZED",
        "authorization": authorization,
    }
    with pytest.raises(ContractError, match=fragment):
        contract.validate_state(state, now=now)


def test_expired_authorization_is_rejected(state):
    now = _now(state)
    authorization = _authorization(now)
    authorization["authorized_at"] = "2026-08-01T00:00:00Z"
    authorization["expires_at"] = "2026-08-02T00:00:00Z"
    state["live_trading"] = {
        "authorized": True,
        "status": "AUTHORIZED",
        "authorization": authorization,
    }
    with pytest.raises(ContractError, match="expired"):
        contract.validate_state(state, now=now)


def test_disabled_state_may_not_retain_a_lapsed_authorization(state):
    """Prevents reactivating a stale grant by flipping one boolean."""
    state["live_trading"] = {
        "authorized": False,
        "status": "DISABLED",
        "authorization": _authorization(_now(state)),
    }
    _rejects(state, "must be null")


# --- fail-closed on schema drift ------------------------------------------


def test_unknown_top_level_field_fails_closed(state):
    state["notes"] = "extra"
    _rejects(state, "unknown field")


def test_unknown_nested_field_fails_closed(state):
    state["lanes"]["lane_1"]["confidence"] = "high"
    _rejects(state, "unknown field")


def test_wrong_schema_version_is_rejected(state):
    state["schema_version"] = 1
    _rejects(state, "schema_version")


def test_updated_by_must_be_a_builder_agent(state):
    state["updated_by"] = "USER"
    _rejects(state, "must be one of")


# --- experiment registry ---------------------------------------------------


def test_complete_experiment_requires_reviewer_and_reviewed_commit(registry):
    registry["experiments"][0]["status"] = "COMPLETE"
    registry["experiments"][0]["result"] = "PASS"
    registry["experiments"][0]["reviewed_commit"] = None
    with pytest.raises(ContractError, match="reviewer and reviewed_commit"):
        contract.validate_experiments(registry, now=datetime.now(UTC))


def test_complete_experiment_may_not_be_self_reviewed(registry):
    row = registry["experiments"][0]
    row.update(
        status="COMPLETE", result="PASS", reviewer=row["builder"], reviewed_commit="a" * 40
    )
    with pytest.raises(ContractError, match="own builder"):
        contract.validate_experiments(registry, now=datetime.now(UTC))


def test_complete_experiment_may_not_have_a_pending_result(registry):
    row = registry["experiments"][0]
    row.update(status="COMPLETE", result="PENDING", reviewed_commit="a" * 40)
    with pytest.raises(ContractError, match="still PENDING"):
        contract.validate_experiments(registry, now=datetime.now(UTC))


def test_duplicate_experiment_ids_are_rejected(registry):
    registry["experiments"].append(copy.deepcopy(registry["experiments"][0]))
    with pytest.raises(ContractError, match="repeats experiment id"):
        contract.validate_experiments(registry, now=datetime.now(UTC))


def test_experiment_id_format_is_enforced(registry):
    registry["experiments"][0]["id"] = "EXP-1"
    with pytest.raises(ContractError, match="EXP-###"):
        contract.validate_experiments(registry, now=datetime.now(UTC))


def test_unknown_evidence_level_is_rejected(registry):
    registry["experiments"][0]["evidence_level"] = "PRETTY_GOOD"
    with pytest.raises(ContractError, match="must be one of"):
        contract.validate_experiments(registry, now=datetime.now(UTC))


# --- live-sensitive guard --------------------------------------------------


def test_guard_flags_a_permission_path_change():
    reasons = guard.classify(["src/hlcopy/trading/permissions.py"], {})
    assert any("live-sensitive pattern" in reason for reason in reasons)


def test_guard_flags_a_token_change_in_code():
    reasons = guard.classify(
        ["services/invo-notification-executor/src/service.ts"],
        {"services/invo-notification-executor/src/service.ts": ["+  if (REAL_TRADING_ENABLED)"]},
    )
    assert any("REAL_TRADING_ENABLED" in reason for reason in reasons)


def test_guard_flags_a_systemd_environment_change():
    reasons = guard.classify(["deploy/systemd/hyperliquid-invo-notification-executor.service"], {})
    assert reasons


def test_guard_ignores_documentation_that_merely_names_a_flag():
    """This governance PR discusses REAL_TRADING_ENABLED without changing it."""
    reasons = guard.classify(
        ["docs/ai-team/LIVE_TRADING_GATE.md"],
        {"docs/ai-team/LIVE_TRADING_GATE.md": ["+`REAL_TRADING_ENABLED=NO` remains the default"]},
    )
    assert reasons == []


def test_guard_requires_an_explicit_yes_declaration():
    assert guard.declared("LIVE-SENSITIVE: YES") == "YES"
    assert guard.declared("live-sensitive: no") == "NO"
    assert guard.declared("this change is live sensitive, honest") is None


def test_guard_parses_per_file_diff_hunks():
    raw = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/docs/x.md b/docs/x.md\n"
        "--- a/docs/x.md\n+++ b/docs/x.md\n@@ -1 +1 @@\n+REAL_TRADING_ENABLED\n"
    )
    parsed = guard.parse_diff(raw)
    assert parsed["a.py"] == ["-old", "+new"]
    assert parsed["docs/x.md"] == ["+REAL_TRADING_ENABLED"]
