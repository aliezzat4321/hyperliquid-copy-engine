import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "repo_context_benchmark.py"
SPEC = importlib.util.spec_from_file_location("repo_context_benchmark", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_local_index_is_exact_sha_keyed_and_bounded():
    index = MODULE.build_local_index("a" * 40)
    assert index["sha"] == "a" * 40
    for case in MODULE.SCENARIOS:
        selected = MODULE.select(index, case["query"], case["required"])
        assert len(selected) <= 4
        assert set(selected).issubset(MODULE.BASELINE_FILES)


def test_benchmark_contains_no_file_contents_or_secret_values():
    index = MODULE.build_local_index("a" * 40)
    assert set(index) == {"schema", "sha", "entries"}
    assert all(set(entry) == {"path", "terms"} for entry in index["entries"])
    flattened = set().union(*(set(entry["terms"]) for entry in index["entries"]))
    assert "real_trading_enabled" not in flattened
    assert "claude_code_oauth_token" not in flattened
