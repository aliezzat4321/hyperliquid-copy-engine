from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

ORCH_PATH = ROOT / "scripts" / "ai_team_orchestrator.py"
ORCH_SPEC = importlib.util.spec_from_file_location("ai_team_orchestrator_manager_test", ORCH_PATH)
assert ORCH_SPEC and ORCH_SPEC.loader
orch = importlib.util.module_from_spec(ORCH_SPEC)
ORCH_SPEC.loader.exec_module(orch)

LEDGER_PATH = ROOT / "scripts" / "ai_team_runtime_ledger.py"
LEDGER_SPEC = importlib.util.spec_from_file_location(
    "ai_team_runtime_ledger_manager_test", LEDGER_PATH
)
assert LEDGER_SPEC and LEDGER_SPEC.loader
runtime_ledger = importlib.util.module_from_spec(LEDGER_SPEC)
LEDGER_SPEC.loader.exec_module(runtime_ledger)


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "base.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)
    return subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()


def _write_protected(path: Path, content: str = "name: harmless\n") -> None:
    target = path / ".github" / "workflows" / "protected.yml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_normal_autonomous_task_still_rejects_owner_sensitive_path(tmp_path: Path) -> None:
    base = _init_repo(tmp_path)
    _write_protected(tmp_path)
    with pytest.raises(RuntimeError, match="owner-sensitive live path"):
        orch.validate_changes(orch.DEFAULT_CONFIG, tmp_path, base)


def test_manager_repair_can_edit_protected_path_but_is_never_auto_mergeable(
    tmp_path: Path,
) -> None:
    base = _init_repo(tmp_path)
    _write_protected(tmp_path)
    files, no_auto = orch.validate_changes(
        orch.DEFAULT_CONFIG,
        tmp_path,
        base,
        allow_owner_sensitive=True,
    )
    assert files == [".github/workflows/protected.yml"]
    assert no_auto is True


def test_manager_repair_does_not_bypass_real_trading_forbidden_scan(tmp_path: Path) -> None:
    base = _init_repo(tmp_path)
    _write_protected(tmp_path, "REAL_TRADING_ENABLED=YES\n")
    with pytest.raises(RuntimeError, match="forbidden live-trading enablement"):
        orch.validate_changes(
            orch.DEFAULT_CONFIG,
            tmp_path,
            base,
            allow_owner_sensitive=True,
        )


def test_every_runtime_event_updates_stable_trello_trigger(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    files = runtime_ledger.RuntimeLedgerFiles(
        root,
        tmp_path / "ledger.sqlite3",
        "aliezzat4321/hyperliquid-copy-engine",
        130,
    )
    first = files.event("TASK_ASSIGNED", issue=146)
    trigger = root / "events" / ".trello-event-trigger"
    assert trigger.exists()
    assert trigger.read_text(encoding="utf-8").strip() == first["at"]

    second = files.event("REVIEW_FAIL", issue=146)
    assert trigger.read_text(encoding="utf-8").strip() == second["at"]


def test_systemd_path_watches_exact_stable_trigger() -> None:
    text = (ROOT / "deploy/systemd/hyperliquid-ai-team-trello-relay.path").read_text(
        encoding="utf-8"
    )
    expected = "PathChanged=/var/lib/hyperliquid-ai-team/events/.trello-event-trigger"
    assert text.count(expected) == 1
    assert ".trello-event-trigger/.trello-event-trigger" not in text


def test_protected_review_uses_canonical_retry_and_repair_ledger() -> None:
    workflow = (ROOT / ".github/workflows/ai-team-manager-protected-review.yml").read_text(
        encoding="utf-8"
    )
    orchestrator = ORCH_PATH.read_text(encoding="utf-8")
    assert "AI_TEAM_MANAGER_PROTECTED=YES" in workflow
    assert "scripts/ai_team_manager_review_enqueue.py" in workflow
    assert "systemctl start hyperliquid-ai-team-orchestrator.service" in workflow
    assert "FAIL_TO_REPAIR_PATH=CLAUDE_REVIEW->CODEX_REPAIR->CLAUDE_REREVIEW" in workflow
    assert "/usr/bin/claude" not in workflow
    assert "gh pr merge" not in workflow
    assert "self.enqueue_repair(task, blockers)" in orchestrator
    assert "owner-sensitive/live path cannot auto-merge" in orchestrator
