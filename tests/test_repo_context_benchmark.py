from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "repo_context_benchmark", ROOT / "scripts/repo_context_benchmark.py"
)
assert SPEC and SPEC.loader
BENCHMARK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK)


def test_percentile_uses_nearest_rank() -> None:
    assert BENCHMARK.percentile([1, 2, 3, 4, 5], 0.9) == 5


def test_graphify_absence_is_durable_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(BENCHMARK.shutil, "which", lambda _: None)
    result = BENCHMARK.graphify_probe()
    assert result["status"] == "UNAVAILABLE"
    assert "no substitute" in result["reason"]


def test_sha_key_rejects_stale_index() -> None:
    assert BENCHMARK.index_is_current("abc", "abc") is True
    assert BENCHMARK.index_is_current("abc", "def") is False
    assert BENCHMARK.index_is_current("", "def") is False


def test_index_does_not_store_literals() -> None:
    symbols = BENCHMARK.safe_symbols("example.py", "TOKEN = 'do-not-index-me'\n")
    assert "token" in symbols
    assert "do-not-index-me" not in symbols


def test_keep_decision_fails_closed_without_graphify(monkeypatch) -> None:
    monkeypatch.setattr(
        BENCHMARK,
        "graphify_probe",
        lambda: {"status": "UNAVAILABLE", "reason": "test"},
    )
    result = BENCHMARK.run()
    assert result["decision"]["keep_added_layer"] is False
    assert result["safety"] == {
        "canonical_source": "git",
        "disposable": True,
        "secrets_stored": False,
    }
    assert result["baseline"]["summary"]["required_file_recall"] == 1.0
    assert result["graphify"]["status"] == "UNAVAILABLE"
