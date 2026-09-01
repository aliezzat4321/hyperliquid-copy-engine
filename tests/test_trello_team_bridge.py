from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "scripts" / "trello_team_bridge.py"
SPEC = importlib.util.spec_from_file_location("trello_team_bridge", PATH)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class FakeTrello:
    def __init__(self, existing: bool = False) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.next_id = "card-146"
        self.existing = existing

    def call(self, method: str, path: str, data: dict | None = None):
        self.calls.append((method, path, data or {}))
        if method == "GET" and path.endswith("/cards"):
            if not self.existing:
                return []
            return [
                {
                    "id": self.next_id,
                    "name": "P0 — VM→Trello (#146)",
                    "desc": "GITHUB: https://github.com/x/y/issues/146",
                    "closed": False,
                }
            ]
        return {"id": self.next_id}


def event(kind: str, **values: object) -> dict:
    return {
        "repository": bridge.REPOSITORY,
        "issue": 146,
        "title": "event-driven Trello board",
        "priority": "P0",
        "event": kind,
        "agent": "CODEX_CHATGPT",
        "task_type": "BUILD",
        "owner": "CODEX_CHATGPT",
        "model": "CODEX_DEFAULT",
        "phase_started_at": "2026-09-01T10:00:00Z",
        "task_started_at": "2026-09-01T09:58:00Z",
        **values,
    }


def test_lifecycle_updates_one_mapped_card_and_material_notifications(tmp_path: Path) -> None:
    client = FakeTrello()
    state = tmp_path / "bridge.json"
    ledger = tmp_path / "missing.sqlite3"
    steps = [
        (event("ASSIGNED", status="RUNNING"), "IN_PROGRESS", False),
        (event("PR_OPENED", pr=22, sha="abc", status="WAITING_CI"), "REVIEW_CI", False),
        (event("REVIEW_PASS", pr=22, result="Claude PASS"), "REVIEW_CI", True),
        (event("BLOCKED", blocker="owner decision needed"), "BLOCKED", True),
        (event("COMPLETED", result="merged and proven"), "DONE", True),
    ]
    for payload, expected_list, notified in steps:
        result = bridge.sync(payload, client, state, ledger)
        assert result == {
            "card_id": "card-146",
            "issue": 146,
            "list": expected_list,
            "notified": notified,
        }
    assert sum(method == "POST" and path == "/cards" for method, path, _ in client.calls) == 1
    updates = sum(
        method == "PUT" and path == "/cards/card-146"
        for method, path, _ in client.calls
    )
    assert updates == 4
    comments = [
        data["text"]
        for _method, path, data in client.calls
        if path.endswith("/actions/comments")
    ]
    assert len(comments) == 3
    assert all(text.startswith("@aliezzat2 ") for text in comments)
    assert json.loads(state.read_text())["cards"][f"{bridge.REPOSITORY}#146"] == "card-146"


def test_existing_manually_seeded_card_is_reused(tmp_path: Path) -> None:
    client = FakeTrello(existing=True)
    state = tmp_path / "bridge.json"
    result = bridge.sync(
        event("BUILD_STARTED", status="RUNNING"),
        client,
        state,
        tmp_path / "missing.sqlite3",
    )
    assert result["card_id"] == "card-146"
    assert not any(method == "POST" and path == "/cards" for method, path, _ in client.calls)
    assert any(method == "PUT" and path == "/cards/card-146" for method, path, _ in client.calls)
    assert json.loads(state.read_text())["cards"][f"{bridge.REPOSITORY}#146"] == "card-146"


def test_active_repair_stays_in_progress_even_with_existing_pr() -> None:
    payload = event("RUN_STARTED", task_type="REPAIR", status="RUNNING", pr=148)
    assert bridge.phase(payload) == "IN_PROGRESS"


def test_external_github_comment_and_ci_converge_on_same_card(tmp_path: Path) -> None:
    client = FakeTrello()
    state = tmp_path / "state.json"
    bridge.sync(
        event("OWNER_ACTION", result="GitHub comment asks for change"),
        client,
        state,
        tmp_path / "x",
    )
    bridge.sync(event("CI_FAIL", pr=55, result="lint failed"), client, state, tmp_path / "x")
    card_calls = [
        path
        for method, path, _ in client.calls
        if method in {"POST", "PUT"} and "/actions/" not in path and not path.endswith("/cards")
    ]
    assert card_calls == ["/cards/card-146"]
    assert client.calls[-2][1] == "/cards/card-146"
    assert client.calls[-2][2]["idList"] == bridge.LISTS["REVIEW_CI"]


def test_card_has_required_fields_fallback_eta_and_over_eta(monkeypatch, tmp_path: Path) -> None:
    now = bridge.dt.datetime(2026, 9, 1, 10, 20, tzinfo=bridge.dt.timezone.utc)
    monkeypatch.setattr(bridge, "utcnow", lambda: now)
    client = FakeTrello()
    bridge.sync(
        event("BUILD_STARTED", next_action="finish tests"),
        client,
        tmp_path / "s",
        tmp_path / "l",
    )
    desc = next(data["desc"] for method, path, data in client.calls if method == "POST" and path == "/cards")
    labels = (
        "Priority", "Issue", "PR / SHA", "Owner", "Reviewer / model", "Status",
        "Latest result", "Blocker", "Next action", "Elapsed time", "ETA band",
        "Expected next checkpoint", "Last updated",
    )
    for label in labels:
        assert f"{label}:" in desc
    assert "5–15 min (conservative fallback)" in desc
    assert "OVER_ETA" in desc


def test_runtime_history_replaces_fallback_after_five_samples(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "ledger.sqlite3")
    db.executescript(
        "CREATE TABLE tasks(id TEXT, task_type TEXT, agent TEXT);"
        "CREATE TABLE runs(id INTEGER, task_id TEXT, started_at TEXT, "
        "ended_at TEXT, exit_code INTEGER);"
    )
    for index, minutes in enumerate((6, 8, 10, 12, 14), 1):
        db.execute("INSERT INTO tasks VALUES(?,?,?)", (str(index), "BUILD", "CODEX_CHATGPT"))
        db.execute(
            "INSERT INTO runs VALUES(?,?,?,?,0)",
            (
                index,
                str(index),
                "2026-09-01T10:00:00Z",
                f"2026-09-01T10:{minutes:02d}:00Z",
            ),
        )
    db.commit()
    db.close()
    now = bridge.dt.datetime(2026, 9, 1, 10, 1, tzinfo=bridge.dt.timezone.utc)
    band, _, _ = bridge.eta(event("BUILD_STARTED"), tmp_path / "ledger.sqlite3", now)
    assert "runtime ledger n=5" in band


def test_sync_failure_record_contains_no_secret_or_payload_text(tmp_path: Path) -> None:
    failure = tmp_path / "failures.jsonl"
    bridge.record_failure(
        failure,
        event("CI_FAIL", blocker="sensitive"),
        RuntimeError("token=secret"),
    )
    text = failure.read_text()
    assert "secret" not in text
    assert "sensitive" not in text
    assert '"issue":146' in text


def test_observation_requires_explicit_estimate() -> None:
    payload = event("SIGNIFICANT_RESULT", agent="SYSTEM", task_type="OBSERVATION")
    value, checkpoint, over = bridge.eta(payload, Path("/missing"), bridge.utcnow())
    assert (value, checkpoint, over) == ("measured estimate required", "not estimated", False)
