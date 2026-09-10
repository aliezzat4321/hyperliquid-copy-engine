import datetime as dt
import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ai_team_external_supervisor.py"
spec = importlib.util.spec_from_file_location("ai_team_external_supervisor", MODULE_PATH)
assert spec and spec.loader
sup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sup)


def body(heartbeat: str, *, codex_status: str = "PENDING", retry_after=None) -> str:
    import json

    payload = {
        "main_head": "abc",
        "pending_owner_action": None,
        "codex": {
            "assignment_id": "c1",
            "issue": 93,
            "status": codex_status,
            "task_type": "BUILD",
            "target_sha": "def",
            "retry_after": retry_after,
            "updated_at": heartbeat,
            "last_progress": heartbeat,
            "blocker": None,
        },
        "claude": {"status": "IDLE"},
        "recovery": {"active_assignment": None},
    }
    return f"```json\n{json.dumps(payload)}\n```\n<!-- AI_TEAM_HEARTBEAT={heartbeat} -->\n"


def healthy_systemd():
    return {
        "timer_enabled": True,
        "timer_active": True,
        "service_active": False,
        "service_age_seconds": 0.0,
    }


def test_parse_runtime_body_extracts_payload_and_heartbeat():
    parsed = sup.parse_runtime_body(body("2026-09-10T15:00:00Z"))
    assert parsed["heartbeat"] == dt.datetime(2026, 9, 10, 15, 0, tzinfo=dt.UTC)
    assert parsed["payload"]["codex"]["issue"] == 93


def test_stale_runtime_status_is_fault_even_when_timer_is_alive():
    now = dt.datetime(2026, 9, 10, 15, 10, tzinfo=dt.UTC)
    parsed = sup.parse_runtime_body(body("2026-09-10T15:00:00Z"))
    fault, detail = sup.determine_fault(
        state={}, parsed=parsed, systemd=healthy_systemd(), now=now
    )
    assert fault == "RUNTIME_STATUS_STALE"
    assert "stale" in detail


def test_timer_down_is_recovered_before_status_logic():
    now = dt.datetime(2026, 9, 10, 15, 0, tzinfo=dt.UTC)
    parsed = sup.parse_runtime_body(body("2026-09-10T15:00:00Z"))
    systemd = healthy_systemd()
    systemd["timer_active"] = False
    fault, _ = sup.determine_fault(state={}, parsed=parsed, systemd=systemd, now=now)
    assert fault == "ORCHESTRATOR_TIMER_DOWN"


def test_hung_service_is_detected_independently_of_fresh_status(monkeypatch):
    monkeypatch.setattr(sup, "SERVICE_HANG_SECONDS", 100)
    now = dt.datetime(2026, 9, 10, 15, 0, tzinfo=dt.UTC)
    parsed = sup.parse_runtime_body(body("2026-09-10T15:00:00Z"))
    systemd = healthy_systemd()
    systemd.update(service_active=True, service_age_seconds=101.0)
    fault, _ = sup.determine_fault(state={}, parsed=parsed, systemd=systemd, now=now)
    assert fault == "ORCHESTRATOR_SERVICE_HUNG"


def test_actionable_no_progress_uses_supervisor_owned_material_clock(monkeypatch):
    monkeypatch.setattr(sup, "NO_PROGRESS_SECONDS", 60)
    now = dt.datetime(2026, 9, 10, 15, 2, tzinfo=dt.UTC)
    parsed = sup.parse_runtime_body(body("2026-09-10T15:02:00Z"))
    fp = sup.fingerprint_material(parsed["payload"])
    state = {
        "material_fingerprint": fp,
        "material_since": "2026-09-10T15:00:00Z",
    }
    fault, detail = sup.determine_fault(
        state=state, parsed=parsed, systemd=healthy_systemd(), now=now
    )
    assert fault == "ACTIONABLE_NO_PROGRESS"
    assert "unchanged" in detail


def test_future_rate_limit_wait_is_not_treated_as_stuck(monkeypatch):
    monkeypatch.setattr(sup, "NO_PROGRESS_SECONDS", 60)
    now = dt.datetime(2026, 9, 10, 15, 2, tzinfo=dt.UTC)
    parsed = sup.parse_runtime_body(
        body(
            "2026-09-10T15:02:00Z",
            codex_status="WAITING_RATE_LIMIT",
            retry_after="2026-09-10T16:00:00Z",
        )
    )
    fp = sup.fingerprint_material(parsed["payload"])
    state = {
        "material_fingerprint": fp,
        "material_since": "2026-09-10T15:00:00Z",
    }
    fault, detail = sup.determine_fault(
        state=state, parsed=parsed, systemd=healthy_systemd(), now=now
    )
    assert fault is None
    assert detail is None


def test_recovery_budget_and_cooldown_are_durable(monkeypatch):
    monkeypatch.setattr(sup, "RECOVERY_COOLDOWN_SECONDS", 180)
    monkeypatch.setattr(sup, "MAX_RECOVERY_ATTEMPTS", 3)
    now = dt.datetime(2026, 9, 10, 15, 0, tzinfo=dt.UTC)
    state = {
        "incident": {
            "fingerprint": "same",
            "attempts": 1,
            "last_recovery_at": "2026-09-10T14:59:00Z",
        }
    }
    allowed, why = sup.recovery_allowed(state, "same", now)
    assert not allowed
    assert why == "recovery cooldown active"

    state["incident"]["attempts"] = 3
    state["incident"]["last_recovery_at"] = "2026-09-10T14:00:00Z"
    allowed, why = sup.recovery_allowed(state, "same", now)
    assert not allowed
    assert why == "recovery budget exhausted"


def test_new_material_resets_no_progress_clock():
    now = dt.datetime(2026, 9, 10, 15, 0, tzinfo=dt.UTC)
    parsed = sup.parse_runtime_body(body("2026-09-10T15:00:00Z"))
    state = {
        "material_fingerprint": "old",
        "material_since": "2026-09-10T14:00:00Z",
    }
    current, since = sup.update_material_clock(state, parsed["payload"], now)
    assert current != "old"
    assert since == now
    assert state["material_since"] == "2026-09-10T15:00:00Z"


def test_incident_fingerprint_ignores_heartbeat_and_transient_service_state():
    first_text = body("2026-09-10T15:00:00Z")
    second_text = first_text.replace(
        "AI_TEAM_HEARTBEAT=2026-09-10T15:00:00Z",
        "AI_TEAM_HEARTBEAT=2026-09-10T15:01:00Z",
    )
    first = sup.parse_runtime_body(first_text)
    second = sup.parse_runtime_body(second_text)
    a = healthy_systemd()
    b = healthy_systemd()
    b["service_active"] = True
    assert sup.incident_fingerprint(
        "ACTIONABLE_NO_PROGRESS", first, a
    ) == sup.incident_fingerprint("ACTIONABLE_NO_PROGRESS", second, b)


def test_deferred_recovery_does_not_burn_budget():
    now = dt.datetime(2026, 9, 10, 15, 0, tzinfo=dt.UTC)
    incident = {"attempts": 2}
    state = {}
    sup.record_recovery_outcome(
        incident, state, outcome_state="DEFERRED", outcome="service active", now=now
    )
    assert incident["attempts"] == 2
    assert "last_recovery_at" not in incident
    assert state["last_error"] is None

# Regression coverage above intentionally keeps external recovery bounded and independent.
