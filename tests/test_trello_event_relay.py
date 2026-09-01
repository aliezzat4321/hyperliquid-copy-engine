from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "scripts" / "trello_event_relay.py"
SPEC = importlib.util.spec_from_file_location("trello_event_relay", PATH)
assert SPEC and SPEC.loader
relay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay)


def ledger(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE tasks(
        id TEXT, issue_number INTEGER, task_type TEXT, agent TEXT,
        model_class TEXT, status TEXT, pr_number INTEGER, target_sha TEXT,
        created_at TEXT, updated_at TEXT, last_error TEXT
        )"""
    )
    db.execute(
        "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "abc",
            146,
            "REPAIR",
            "CODEX_CHATGPT",
            "CODEX_DEFAULT",
            "RUNNING",
            148,
            "deadbeef",
            "2026-09-01T10:00:00Z",
            "2026-09-01T10:05:00Z",
            None,
        ),
    )
    db.commit()
    db.close()


def test_initialize_starts_at_existing_event_eof(tmp_path: Path) -> None:
    events = tmp_path / "events"
    events.mkdir()
    event_file = events / "2026-09-01.jsonl"
    event_file.write_text('{"event":"OLD"}\n', encoding="utf-8")
    cursor = tmp_path / "cursor.json"
    assert relay.initialize(events, cursor) == 0
    assert json.loads(cursor.read_text())[event_file.name] == event_file.stat().st_size


def test_relay_advances_only_after_success(monkeypatch, tmp_path: Path) -> None:
    events = tmp_path / "events"
    events.mkdir()
    event_file = events / "2026-09-01.jsonl"
    event_file.write_text(
        json.dumps(
            {
                "event": "RUN_STARTED",
                "issue": 146,
                "assignment_id": "abc",
                "at": "2026-09-01T10:05:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "ledger.sqlite3"
    ledger(db_path)
    cursor = tmp_path / "cursor.json"
    monkeypatch.setattr(relay, "issue_metadata", lambda _issue: ("P0: Trello", "P0"))
    seen: list[dict] = []
    monkeypatch.setattr(relay, "bridge", lambda payload, _path: seen.append(payload) or True)
    assert relay.relay_once(events, cursor, db_path, tmp_path / "bridge.py") == 0
    assert seen[0]["task_type"] == "REPAIR"
    assert seen[0]["status"] == "RUNNING"
    assert json.loads(cursor.read_text())[event_file.name] == event_file.stat().st_size


def test_deferred_sync_keeps_event_for_retry(monkeypatch, tmp_path: Path) -> None:
    events = tmp_path / "events"
    events.mkdir()
    event_file = events / "2026-09-01.jsonl"
    event_file.write_text('{"event":"TASK_BLOCKED","issue":146}\n', encoding="utf-8")
    cursor = tmp_path / "cursor.json"
    monkeypatch.setattr(relay, "issue_metadata", lambda _issue: ("P0: Trello", "P0"))
    monkeypatch.setattr(relay, "bridge", lambda _payload, _path: False)
    assert relay.relay_once(events, cursor, tmp_path / "none.sqlite3", tmp_path / "b") == 75
    assert json.loads(cursor.read_text()).get(event_file.name, 0) == 0
