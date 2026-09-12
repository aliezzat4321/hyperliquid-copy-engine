#!/usr/bin/env python3
"""Reproducible, fail-open repository-context benchmark for Issue #150.

The benchmark never changes canonical repository data.  Its local index is keyed by the
current git SHA and discarded after the run.  Graphify is measured only when its CLI is
already installed; absence is recorded rather than replaced with a look-alike.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*")
TEXT_SUFFIXES = {".md", ".py", ".json", ".toml", ".sh", ".yml", ".yaml"}


WORKLOADS = (
    {
        "id": "review-routing",
        "query": "Claude exact SHA review routing model budgets protected paths",
        "required": {
            "scripts/ai_team_orchestrator.py",
            "config/ai_team_router.json",
            "tests/test_ai_team_orchestrator.py",
        },
    },
    {
        "id": "runtime-ledger",
        "query": "durable runtime ledger acceptance checkpoint recovery",
        "required": {
            "scripts/ai_team_runtime_ledger.py",
            "tests/test_ai_team_runtime_ledger.py",
        },
    },
    {
        "id": "team-contract",
        "query": "AI team contract state validation live authorization fail closed",
        "required": {
            "scripts/ai_team_contract.py",
            "tests/test_ai_team_contract_v2.py",
            "docs/ai-team/LIVE_TRADING_GATE.md",
        },
    },
)


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


def tracked_text() -> dict[str, str]:
    files: dict[str, str] = {}
    for rel in _git("ls-files").splitlines():
        path = ROOT / rel
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        try:
            files[rel] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
    return files


def tokens(text: str) -> int:
    """Deterministic input-token proxy; deliberately labelled as an estimate."""
    return len(re.findall(r"\w+|[^\w\s]", text, re.UNICODE))


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.999999))]


def index_is_current(index_sha: str, repository_sha: str) -> bool:
    """Reject stale indexes rather than returning potentially incorrect context."""
    return bool(index_sha) and index_sha == repository_sha


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "median_input_tokens_estimate": statistics.median(
            s["input_tokens_estimate"] for s in samples
        ),
        "p90_input_tokens_estimate": percentile(
            [s["input_tokens_estimate"] for s in samples], 0.9
        ),
        "median_cached_input_tokens_estimate": statistics.median(
            s["cached_input_tokens_estimate"] for s in samples
        ),
        "median_latency_ms": round(statistics.median(s["latency_ms"] for s in samples), 3),
        "p90_latency_ms": round(percentile([s["latency_ms"] for s in samples], 0.9), 3),
        "required_file_recall": round(
            sum(s["required_file_recall"] for s in samples) / len(samples), 4
        ),
        "stale_context_errors": sum(s["stale_context_errors"] for s in samples),
    }


def baseline(files: dict[str, str]) -> tuple[list[dict[str, Any]], int]:
    context = "\n".join(files.values())
    count = tokens(context)
    samples = [
        {
            "workload": task["id"],
            "input_tokens_estimate": count,
            "cached_input_tokens_estimate": 0,
            "latency_ms": 0.0,
            "required_file_recall": 1.0,
            "stale_context_errors": 0,
        }
        for task in WORKLOADS
    ]
    return samples, len(context.encode())


def safe_symbols(rel: str, text: str) -> set[str]:
    """Index paths and Python syntax names, never literals or file contents."""
    result = {token.lower() for token in WORD.findall(rel)}
    if not rel.endswith(".py"):
        return result
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return result
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            result.add(node.name.lower())
        elif isinstance(node, ast.Name):
            result.add(node.id.lower())
        elif isinstance(node, ast.Import):
            result.update(alias.name.lower() for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module.lower())
    return result


def local_index(files: dict[str, str], sha: str) -> tuple[list[dict[str, Any]], int, float]:
    started = time.perf_counter()
    index = {rel: safe_symbols(rel, text) for rel, text in files.items()}
    serialized = json.dumps(
        {"sha": sha, "symbols": {key: sorted(value) for key, value in index.items()}},
        sort_keys=True,
    ).encode()
    build_ms = (time.perf_counter() - started) * 1000
    if not index_is_current(sha, _git("rev-parse", "HEAD")):
        raise RuntimeError("stale local context index")
    samples: list[dict[str, Any]] = []
    for task in WORKLOADS:
        query = {token.lower() for token in WORD.findall(task["query"])}
        before = time.perf_counter()
        ranked = sorted(index, key=lambda rel: (-len(query & index[rel]), rel))[:12]
        elapsed = (time.perf_counter() - before) * 1000
        selected = set(ranked)
        context = "\n".join(files[rel] for rel in ranked)
        samples.append(
            {
                "workload": task["id"],
                "input_tokens_estimate": tokens(context),
                "cached_input_tokens_estimate": len(serialized) // 4,
                "latency_ms": round(elapsed, 3),
                "required_file_recall": len(task["required"] & selected) / len(task["required"]),
                "stale_context_errors": 0,
            }
        )
    return samples, len(serialized), build_ms


def graphify_probe() -> dict[str, Any]:
    executable = shutil.which("graphify")
    if executable is None:
        return {
            "status": "UNAVAILABLE",
            "reason": "graphify executable is not installed; no substitute was used",
        }
    started = time.perf_counter()
    probe = subprocess.run(
        [executable, "--version"], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    return {
        "status": "AVAILABLE" if probe.returncode == 0 else "ERROR",
        "version": (probe.stdout or probe.stderr).strip()[:300],
        "probe_latency_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def run() -> dict[str, Any]:
    sha = _git("rev-parse", "HEAD")
    files = tracked_text()
    baseline_samples, baseline_bytes = baseline(files)
    local_samples, index_bytes, build_ms = local_index(files, sha)
    baseline_summary = summarize(baseline_samples)
    local_summary = summarize(local_samples)
    reduction = 1 - (
        local_summary["median_input_tokens_estimate"]
        / baseline_summary["median_input_tokens_estimate"]
    )
    correctness_ok = (
        local_summary["required_file_recall"] == 1.0
        and local_summary["stale_context_errors"] == 0
    )
    graphify = graphify_probe()
    keep_local = reduction >= 0.4 and correctness_ok
    return {
        "schema_version": 1,
        "issue": 150,
        "repository": "aliezzat4321/hyperliquid-copy-engine",
        "sha": sha,
        "token_measurement": "deterministic lexical input-token estimate (not provider billing)",
        "cache_model": "cold baseline; warm SHA-keyed local index serialized bytes / 4",
        "baseline": {
            "status": "MEASURED",
            "summary": baseline_summary,
            "context_bytes": baseline_bytes,
        },
        "local_sha_index": {
            "status": "MEASURED",
            "summary": local_summary,
            "index_bytes": index_bytes,
            "build_latency_ms": round(build_ms, 3),
            "median_input_reduction": round(reduction, 4),
        },
        "graphify": graphify,
        "decision": {
            "keep_added_layer": False,
            "local_threshold_pass": keep_local,
            "reason": (
                "No layer is retained: Graphify was not measurable in this environment. "
                + (
                    "The local index passed its proxy gate, but the required three-way "
                    "benchmark is incomplete."
                    if keep_local
                    else "The local index did not pass the 40% and correctness gate."
                )
            ),
        },
        "safety": {"canonical_source": "git", "disposable": True, "secrets_stored": False},
        "limitations": [
            "Input and cached-input counts are deterministic lexical/index-size proxies, "
            "not provider billing telemetry.",
            "Graphify metrics cannot be collected until the Graphify CLI is present in the runner.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=args.output.parent, delete=False) as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
