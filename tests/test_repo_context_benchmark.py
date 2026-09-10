import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "repo_context_benchmark", Path(__file__).parents[1] / "scripts/repo_context_benchmark.py"
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def test_sha_index_is_bounded_and_stale_state_is_detectable(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("def alpha(): pass\n")
    (tmp_path / "src/b.py").write_text("def beta(): pass\n")
    monkeypatch.setattr(bench, "tracked_text", lambda _: ["src/a.py", "src/b.py"])
    sha = "1" * 40
    index, _ = bench.build_local_index(tmp_path, sha, tmp_path / "indexes" / f"{sha}.json")
    assert bench.local_select(index, "repair src/a.py alpha", 1) == ["src/a.py"]
    assert index["sha"] == sha
    assert index["sha"] != "0" * 40


def test_report_fails_open_when_graphify_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(bench.shutil, "which", lambda _: None)
    result = bench.unavailable_graphify()
    assert result["status"] == "UNAVAILABLE"
    assert result["samples"] == 0
    assert "no network install" in result["reason"]
