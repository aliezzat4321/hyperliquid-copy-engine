from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_WORKFLOW = REPO_ROOT / ".github/workflows/deploy-ai-team-orchestrator.yml"
BRIDGE_PATH = "scripts/trello_team_bridge.py"


def test_trello_bridge_changes_trigger_deploy_and_are_compiled_before_install() -> None:
    """Bridge-only merges must deploy the same bridge that the installer copies."""
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert f'- "{BRIDGE_PATH}"' in workflow
    assert (
        "python3 -m py_compile "
        "scripts/ai_team_orchestrator.py scripts/ai_team_runtime_ledger.py "
        f"{BRIDGE_PATH}"
    ) in workflow

