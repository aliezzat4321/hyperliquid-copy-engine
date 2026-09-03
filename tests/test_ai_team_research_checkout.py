import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ai_team_orchestrator as orch


def test_prepare_checkout_allows_read_only_call_without_branch(tmp_path, monkeypatch):
    base = tmp_path / "work"
    workdir = base / "research-task"
    workdir.mkdir(parents=True)

    monkeypatch.setattr(
        orch, "normalize_worktree_ownership", lambda path, user: (1000, 1000)
    )

    class CP:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(orch, "run", lambda *args, **kwargs: CP())

    result = orch.prepare_checkout(
        user="nobody",
        home=tmp_path / "home",
        base_dir=base,
        task_id="research-task",
        ref="origin/main",
    )
    assert result == workdir
