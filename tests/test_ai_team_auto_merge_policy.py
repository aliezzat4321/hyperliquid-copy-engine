import json
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "ai_team_orchestrator", ROOT / "scripts" / "ai_team_orchestrator.py"
)
assert spec and spec.loader
orch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orch)

EXPECTED = {
    "ROUTINE",
    "QUANT_PROFITABILITY",
    "STATISTICAL_METHODOLOGY",
    "MAJOR_ARCHITECTURE",
    "UNRESOLVED_DISAGREEMENT",
    "CAPITAL_SENSITIVE_METHODOLOGY",
}

def test_all_recognized_task_classes_auto_merge_after_review_and_ci():
    cfg = json.loads((ROOT / "config" / "ai_team_router.json").read_text())
    assert set(cfg["auto_merge_task_classes"]) == EXPECTED
    assert set(orch.DEFAULT_CONFIG["auto_merge_task_classes"]) == EXPECTED

def test_fallback_protected_actions_fail_closed_until_explicitly_configured():
    cfg = json.loads((ROOT / "config" / "ai_team_router.json").read_text())
    assert cfg["remediation"]["protected_actions"] == {}
    assert orch.DEFAULT_CONFIG["remediation"]["protected_actions"] == {}
