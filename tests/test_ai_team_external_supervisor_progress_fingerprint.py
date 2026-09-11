import datetime as dt
import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ai_team_external_supervisor.py"
SPEC = importlib.util.spec_from_file_location("ai_team_external_supervisor_progress", MODULE_PATH)
assert SPEC and SPEC.loader
sup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sup)


def payload(*, updated_at: str, last_progress: str, retry_after: str) -> dict[str, object]:
    return {
        "main_head": "abc",
        "pending_owner_action": None,
        "codex": {
            "assignment_id": "c1",
            "issue": 93,
            "pr": None,
            "status": "RETRY",
            "task_type": "PRODUCTION_VALIDATION",
            "target_sha": "def",
            "retry_after": retry_after,
            "updated_at": updated_at,
            "last_progress": last_progress,
            "blocker": "awaiting deterministic phase runner evidence",
        },
        "claude": {"status": "IDLE"},
        "recovery": {"active_assignment": None},
    }


def healthy_systemd() -> dict[str, object]:
    return {
        "timer_enabled": True,
        "timer_active": True,
        "service_active": False,
        "service_age_seconds": 0.0,
    }


def test_timestamp_and_retry_refresh_cannot_hide_actionable_no_progress(monkeypatch) -> None:
    monkeypatch.setattr(sup, "NO_PROGRESS_SECONDS", 60)
    first = payload(
        updated_at="2026-09-10T15:00:00Z",
        last_progress="2026-09-10T15:00:00Z",
        retry_after="2026-09-10T14:59:00Z",
    )
    refreshed = payload(
        updated_at="2026-09-10T15:02:00Z",
        last_progress="2026-09-10T15:00:00Z",
        retry_after="2026-09-10T15:01:00Z",
    )
    assert sup.fingerprint_material(first) == sup.fingerprint_material(refreshed)

    now = dt.datetime(2026, 9, 10, 15, 2, tzinfo=sup.UTC)
    state = {
        "material_fingerprint": sup.fingerprint_material(first),
        "material_since": "2026-09-10T15:00:00Z",
    }
    fault, detail = sup.determine_fault(
        state=state,
        parsed={"heartbeat": now, "payload": refreshed},
        systemd=healthy_systemd(),
        now=now,
    )
    assert fault == "ACTIONABLE_NO_PROGRESS"
    assert detail == "actionable runtime material unchanged for 120s"
    assert state["material_since"] == "2026-09-10T15:00:00Z"


def test_true_last_progress_transition_resets_supervisor_material_clock() -> None:
    first = payload(
        updated_at="2026-09-10T15:00:00Z",
        last_progress="2026-09-10T15:00:00Z",
        retry_after="2026-09-10T14:59:00Z",
    )
    progressed = payload(
        updated_at="2026-09-10T15:02:00Z",
        last_progress="2026-09-10T15:02:00Z",
        retry_after="2026-09-10T15:01:00Z",
    )
    now = dt.datetime(2026, 9, 10, 15, 2, tzinfo=sup.UTC)
    state = {
        "material_fingerprint": sup.fingerprint_material(first),
        "material_since": "2026-09-10T14:00:00Z",
    }

    current, since = sup.update_material_clock(state, progressed, now)

    assert current != sup.fingerprint_material(first)
    assert since == now
    assert state["material_since"] == "2026-09-10T15:02:00Z"
