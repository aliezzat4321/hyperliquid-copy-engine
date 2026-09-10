import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lane1_runtime_acceptance.py"
SPEC = importlib.util.spec_from_file_location("lane1_runtime_acceptance", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_observer_persists_complete_current_boundary_counts(tmp_path: Path) -> None:
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    stamp = now.isoformat()
    universe, funnel, queue, prospective = [tmp_path / name for name in ("u", "f", "q", "p")]
    _write(universe, {"generated_at": stamp, "real_trading": False})
    _write(funnel, {"run_at": stamp, "real_trading": False, "boundary_counts": {
        "fetched": 10, "new_or_changed": 2, "profiled": 8, "screened": 7, "robust": 3}})
    _write(queue, {"generated_at": stamp, "real_trading": False,
                   "counts": {"robust": 3, "challenger": 1, "demoted": 2}})
    _write(prospective, {"observed_at": stamp, "real_trading": False,
        "prospective_shadow_count": 1, "shadow_decision_count": 5,
        "shadow_execution_count": 4, "shadow_reject_count": 1,
        "rejections": [{"reason": "depth"}],
        "targets": [{"scenarios": [{"realized_actions": 4}]}]})
    result = MODULE.observe(universe_path=universe, funnel_path=funnel, queue_path=queue,
        prospective_path=prospective, output_path=tmp_path / "out.json",
        max_age_minutes=30, now=now)
    assert result["runtime_healthy"] is True
    assert result["counts"] == {"fetched": 10, "new_or_changed": 2, "profiled": 8,
        "screened": 7, "robust": 3, "challenger": 1, "prospective_shadow": 1,
        "shadow_decisions": 5, "shadow_executions": 4, "shadow_rejects": 1,
        "demoted": 2}


def test_observer_writes_durable_failure_evidence_when_inputs_missing(tmp_path: Path) -> None:
    output = tmp_path / "out.json"
    result = MODULE.observe(universe_path=tmp_path / "u", funnel_path=tmp_path / "f",
        queue_path=tmp_path / "q", prospective_path=tmp_path / "p",
        output_path=output, max_age_minutes=30,
        now=datetime(2026, 9, 10, 12, tzinfo=UTC))
    assert result["runtime_healthy"] is False
    assert len(result["blockers"]) >= 4
    assert json.loads(output.read_text(encoding="utf-8")) == result

