import datetime as dt
import importlib.util
import subprocess
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ai_team_orchestrator.py"
spec = importlib.util.spec_from_file_location("ai_team_watchdog", MODULE_PATH)
orch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orch)


def old(seconds=900):
    value = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Runtime:
    def __init__(self):
        self.events = []

    def event(self, kind, **payload):
        self.events.append((kind, payload))


class GH:
    def __init__(self, pr_state="open", checks="PENDING", merged_at=None):
        self.pr_state, self.checks, self.merged_at, self.comments = (
            pr_state, checks, merged_at, []
        )

    def pr(self, _number):
        return {
            "state": self.pr_state,
            "merged_at": self.merged_at,
            "head": {"sha": "a" * 40},
        }

    def check_state(self, _sha):
        return self.checks, "test"

    def comment(self, issue, body):
        self.comments.append((issue, body))


def team(tmp_path, gh=None, **watchdog):
    value = object.__new__(orch.Orchestrator)
    value.cfg = {**orch.DEFAULT_CONFIG, "watchdog": {
        **orch.DEFAULT_CONFIG["watchdog"], **watchdog,
    }}
    value.ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    value.gh, value.runtime = gh or GH(), Runtime()
    value.reap_stale_child = lambda _task: None
    return value


def test_closed_waiting_ci_is_staled_and_claim_released(tmp_path):
    value = team(tmp_path, GH(pr_state="closed"))
    task_id = value.ledger.create_task(
        issue_number=205, pr_number=209, target_sha="a" * 40,
        task_type="REVIEW", agent="CLAUDE", model_class="SONNET",
        task_class="ROUTINE", status="WAITING_CI",
    )
    value.watchdog()
    assert value.ledger.get(task_id)["status"] == "STALE"
    assert value.ledger.has_queue_claim_conflict() is False


def test_merged_waiting_ci_is_not_staled_or_claim_released(tmp_path):
    value = team(
        tmp_path,
        GH(pr_state="closed", merged_at="2026-01-01T00:00:00Z"),
    )
    task_id = value.ledger.create_task(
        issue_number=205, pr_number=209, target_sha="a" * 40,
        task_type="REVIEW", agent="CLAUDE", model_class="SONNET",
        task_class="ROUTINE", status="WAITING_CI",
    )
    value.watchdog()
    assert value.ledger.get(task_id)["status"] == "WAITING_CI"
    assert value.ledger.has_queue_claim_conflict() is True


def test_identical_material_loop_opens_deduplicated_stalled_alert(tmp_path):
    value = team(tmp_path, no_progress_cycles=2)
    value.ledger.create_task(
        issue_number=226, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="PENDING",
    )
    value.watchdog()
    value.watchdog()
    value.watchdog()
    assert len(value.gh.comments) == 1
    assert "WATCHDOG_ALERT=OPEN" in value.gh.comments[0][1]
    assert value.ledger.watchdog_snapshot()["active_alerts"][0]["kind"] == "NO_PROGRESS"


def test_stale_runnable_recovers_then_alerts_after_bounded_repeat(tmp_path):
    value = team(tmp_path, runnable_stale_seconds=1, max_recovery_attempts=2)
    task_id = value.ledger.create_task(
        issue_number=226, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="PENDING",
    )
    value.ledger.db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old(), task_id))
    value.ledger.db.commit()
    value.watchdog()
    assert value.ledger.get(task_id)["status"] == "RETRY"
    value.ledger.db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old(), task_id))
    value.ledger.db.commit()
    value.watchdog()
    assert len(value.gh.comments) == 1
    assert "OWNER_ACTION_REQUIRED" in value.gh.comments[0][1]


def test_expired_claude_wait_resumes_without_claiming_codex_slot(tmp_path):
    value = team(tmp_path, rate_limit_resume_stale_seconds=1)
    task_id = value.ledger.create_task(
        issue_number=208, task_type="REVIEW", agent="CLAUDE", model_class="SONNET",
        task_class="ROUTINE", status="WAITING_RATE_LIMIT", retry_at=old(),
    )
    value.watchdog()
    assert value.ledger.get(task_id)["status"] == "RETRY"


def test_status_mirror_staleness_is_separate_and_alert_is_deduplicated(tmp_path):
    value = team(tmp_path, status_mirror_stale_seconds=1)
    value.ledger.heartbeat("status_mirror", old())
    value.watchdog()
    value.watchdog()
    alerts = value.ledger.watchdog_snapshot()["active_alerts"]
    assert [a["kind"] for a in alerts] == ["STATUS_MIRROR"]
    assert len(value.gh.comments) == 1


def test_completed_ci_not_consumed_is_requeued(tmp_path):
    value = team(tmp_path, GH(checks="PASS"), ci_consumption_stale_seconds=1)
    task_id = value.ledger.create_task(
        issue_number=195, pr_number=199, target_sha="a" * 40,
        task_type="REVIEW", agent="CLAUDE", model_class="SONNET",
        task_class="ROUTINE", status="WAITING_CI",
    )
    value.ledger.db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old(), task_id))
    value.ledger.db.commit()
    value.watchdog()
    assert value.ledger.get(task_id)["status"] == "RETRY"


def test_github_5xx_retries_are_bounded(monkeypatch):
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        code = 0 if len(calls) == 3 else 1
        return subprocess.CompletedProcess(cmd, code, '{}' if code == 0 else '',
                                           '' if code == 0 else 'HTTP 503')

    monkeypatch.setattr(orch, "run", fake_run)
    monkeypatch.setattr(orch.time, "sleep", lambda _seconds: None)
    assert orch.GitHub(orch.REPO).issue(226) == {}
    assert len(calls) == 3
