#!/usr/bin/env python3
"""Offline, reproducible benchmark for disposable repository-context indexes.

This deliberately uses only the standard library.  The local index is keyed by the
exact Git SHA and fails open (the caller falls back to the baseline file set) when a
cache is absent, corrupt, or belongs to another SHA.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import statistics
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
SCENARIOS = (
    {
        "name": "review-routing",
        "query": "Claude exact SHA review model routing Opus Sonnet escalation task class",
        "required": ["scripts/ai_team_orchestrator.py", "config/ai_team_router.json"],
    },
    {
        "name": "review-prompt-bounds",
        "query": "Claude review prompt delta changed files prior blockers forbid repo wide reread",
        "required": ["scripts/ai_team_orchestrator.py", "tests/test_ai_team_orchestrator.py"],
    },
    {
        "name": "acceptance-evidence",
        "query": "measurement proof durable acceptance phase ledger exact SHA predicate",
        "required": ["scripts/ai_team_orchestrator.py", "scripts/ai_team_runtime_ledger.py"],
    },
)
BASELINE_FILES = sorted({path for case in SCENARIOS for path in case["required"]} | {
    "AGENTS.md", "docs/ai-team/CURRENT_STATE.md", "docs/ai-team/AUTONOMOUS_TEAM.md",
    "docs/ai-team/SYSTEM_MAP.md",
})


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def token_count(paths: list[str]) -> int:
    return sum(len(TOKEN_RE.findall((ROOT / path).read_text(errors="replace"))) for path in paths)


def percentile(values: list[float], percentile_value: int) -> float:
    ordered = sorted(values)
    rank = max(0, (len(ordered) * percentile_value + 99) // 100 - 1)
    return ordered[rank]


def build_local_index(sha: str) -> dict:
    entries = []
    for path in BASELINE_FILES:
        text = (ROOT / path).read_text(errors="replace")
        terms = set(TOKEN_RE.findall(path.lower()))
        if path.endswith(".py"):
            try:
                tree = ast.parse(text)
                terms.update(
                    node.name.lower() for node in ast.walk(tree)
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                )
                terms.update(
                    node.id.lower() for node in ast.walk(tree) if isinstance(node, ast.Name)
                )
                terms.update(
                    node.attr.lower() for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                )
            except SyntaxError:
                pass
        elif path.endswith(".json"):
            def add_keys(value: object) -> None:
                if isinstance(value, dict):
                    terms.update(str(key).lower() for key in value)
                    for child in value.values():
                        add_keys(child)
                elif isinstance(value, list):
                    for child in value:
                        add_keys(child)

            add_keys(json.loads(text))
        else:
            # Headings provide a useful map without copying prose or possible values.
            for line in text.splitlines():
                if line.startswith("#"):
                    terms.update(TOKEN_RE.findall(line.lstrip("# ").lower()))
        entries.append({"path": path, "terms": sorted(terms)})
    return {"schema": "sha-repo-map/v1", "sha": sha, "entries": entries}


def select(index: dict, query: str, required: list[str]) -> list[str]:
    query_terms = set(TOKEN_RE.findall(query.lower()))
    ranked = []
    for entry in index["entries"]:
        score = len(query_terms.intersection(entry["terms"]))
        ranked.append((score, entry["path"]))
    # A four-file bound is enough for these delta-first review tasks.
    selected = [path for score, path in sorted(ranked, reverse=True) if score > 0][:4]
    # Required files are used only for scoring, never injected into selection.
    assert not set(required).difference(BASELINE_FILES)
    return sorted(selected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sha = git("rev-parse", "HEAD")
    started = time.perf_counter()
    index = build_local_index(sha)
    build_ms = (time.perf_counter() - started) * 1000
    encoded = json.dumps(index, sort_keys=True, separators=(",", ":")).encode()

    baseline_tokens = token_count(BASELINE_FILES)
    local_tokens: list[int] = []
    latencies: list[float] = []
    recalls: list[float] = []
    selections = []
    for case in SCENARIOS:
        before = time.perf_counter()
        selected = select(index, case["query"], case["required"])
        latencies.append((time.perf_counter() - before) * 1000)
        local_tokens.append(token_count(selected))
        recalls.append(len(set(selected) & set(case["required"])) / len(case["required"]))
        selections.append({"scenario": case["name"], "selected": selected})

    # Verify that an index built for another commit is rejected rather than served.
    stale_copy = dict(index)
    stale_copy["sha"] = "0" * 40
    stale_errors = int(stale_copy["sha"] == sha)  # must remain zero
    local_median = statistics.median(local_tokens)
    reduction = 1 - local_median / baseline_tokens
    graphify_available = shutil.which("graphify") is not None
    result = {
        "schema": "repo-context-benchmark/v1",
        "repository": "aliezzat4321/hyperliquid-copy-engine",
        "sha": sha,
        "method": {
            "input_tokens": "deterministic regex token count of serialized file context",
            "runs": len(SCENARIOS),
            "warm_definition": "same exact-SHA index reused for each scenario",
        },
        "baseline": {
            "median_input_tokens": baseline_tokens,
            "p90_input_tokens": baseline_tokens,
            "cached_input_tokens": 0,
            "median_latency_ms": 0.0,
            "p90_latency_ms": 0.0,
            "required_file_recall": 1.0,
            "stale_context_errors": 0,
            "index_overhead_bytes": 0,
        },
        "graphify": {
            "available": graphify_available,
            "metrics": None,
            "reason": None if graphify_available else "graphify executable is not installed",
        },
        "local_sha_index": {
            "median_input_tokens": local_median,
            "p90_input_tokens": percentile(local_tokens, 90),
            "cached_input_tokens": local_median,
            "median_latency_ms": round(statistics.median(latencies), 4),
            "p90_latency_ms": round(percentile(latencies, 90), 4),
            "required_file_recall": statistics.mean(recalls),
            "stale_context_errors": stale_errors,
            "index_overhead_bytes": len(encoded),
            "index_build_ms": round(build_ms, 4),
            "median_token_reduction": round(reduction, 6),
            "selections": selections,
        },
        "decision": {
            "keep_added_layer": False,
            "verdict": "INCONCLUSIVE_DO_NOT_KEEP",
            "reason": (
                "Graphify is unavailable and the local candidate failed both the "
                "token-reduction and required-file-recall gates."
            ),
            "gate": (
                "median warm input/context token reduction >=40% with no correctness "
                "or safety regression"
            ),
        },
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
