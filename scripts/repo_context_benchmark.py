#!/usr/bin/env python3
"""Benchmark disposable repository-context strategies for Issue #150.

The runner is deliberately offline and read-only.  Git remains canonical; generated
indexes are SHA-keyed JSON files under the selected output directory and may be deleted.
Token counts are deterministic UTF-8 byte estimates (ceil(bytes / 4)), not provider
billing counters, and are labelled as such in the report.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA = "repo-context-benchmark/v1"
TEXT_SUFFIXES = {".md", ".py", ".json", ".toml", ".yaml", ".yml", ".sh", ".ts"}
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-/]{2,}")
SYMBOL = re.compile(r"(?:def|class|function|interface|type)\s+([A-Za-z_][A-Za-z0-9_]*)")
DEPENDENCY = re.compile(
    r"(?:from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import|import\s+['\"]?([A-Za-z_][A-Za-z0-9_./-]*))"
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def tracked_text(root: Path) -> list[str]:
    files = git(root, "ls-files").splitlines()
    return sorted(p for p in files if Path(p).suffix.lower() in TEXT_SUFFIXES)


def estimate_tokens(root: Path, paths: list[str]) -> int:
    size = sum(len((root / path).read_bytes()) for path in paths if (root / path).is_file())
    return math.ceil(size / 4)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def build_local_index(root: Path, sha: str, output: Path) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    entries: dict[str, dict[str, list[str]]] = {}
    for path in tracked_text(root):
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        terms = {term.lower() for term in TOKEN.findall(path + "\n" + text)}
        dependencies = {left or right for left, right in DEPENDENCY.findall(text)}
        entries[path] = {
            "terms": sorted(terms),
            "symbols": sorted(set(SYMBOL.findall(text))),
            "dependencies": sorted(dependencies),
        }
    payload = {"schema": "sha-repo-map/v1", "sha": sha, "entries": entries}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return payload, (time.perf_counter() - started) * 1000


def local_select(index: dict[str, Any], query: str, limit: int) -> list[str]:
    wanted = {term.lower() for term in TOKEN.findall(query)}
    ranked = []
    for path, entry in index["entries"].items():
        path_lower = path.lower()
        term_set = set(entry["terms"])
        score = len(wanted & term_set) + 4 * sum(
            1 for term in wanted if term in path_lower
        )
        if score:
            ranked.append((-score, path))
    return [path for _, path in sorted(ranked)[:limit]]


def summarize(
    rows: list[dict[str, Any]], overhead_ms: float, overhead_bytes: int
) -> dict[str, Any]:
    tokens = [float(row["input_tokens_estimate"]) for row in rows]
    cached = [float(row["cached_input_tokens_estimate"]) for row in rows]
    latency = [float(row["latency_ms"]) for row in rows]
    recall = [float(row["required_file_recall"]) for row in rows]
    return {
        "status": "MEASURED",
        "samples": len(rows),
        "median_input_tokens_estimate": statistics.median(tokens),
        "p90_input_tokens_estimate": percentile(tokens, .9),
        "median_cached_input_tokens_estimate": statistics.median(cached),
        "p90_cached_input_tokens_estimate": percentile(cached, .9),
        "median_latency_ms": statistics.median(latency),
        "p90_latency_ms": percentile(latency, .9),
        "required_file_recall": statistics.mean(recall),
        "stale_context_errors": sum(bool(row["stale_context_error"]) for row in rows),
        "index_overhead_ms": overhead_ms,
        "index_overhead_bytes": overhead_bytes,
        "tasks": rows,
    }


def unavailable_graphify() -> dict[str, Any]:
    executable = shutil.which("graphify")
    return {
        "status": "UNAVAILABLE" if executable is None else "UNSUPPORTED_INTERFACE",
        "executable": executable,
        "reason": (
            "no graphify executable is installed; no network install was attempted"
            if executable is None else
            "graphify executable exists but this repository defines no pinned adapter/CLI contract"
        ),
        "samples": 0,
    }


def run(root: Path, workload_path: Path, output: Path, index_dir: Path) -> dict[str, Any]:
    sha = git(root, "rev-parse", "HEAD")
    workload = json.loads(workload_path.read_text(encoding="utf-8"))
    index_path = index_dir / f"{sha}.json"
    index, overhead_ms = build_local_index(root, sha, index_path)
    all_files = tracked_text(root)
    baseline_rows, local_rows = [], []
    for task in workload["tasks"]:
        required = set(task["required_files"])
        for name, selected, rows in (
            ("baseline", all_files, baseline_rows),
            (
                "local_sha_index",
                local_select(index, task["query"], workload["max_files"]),
                local_rows,
            ),
        ):
            started = time.perf_counter()
            count = estimate_tokens(root, selected)
            latency_ms = (time.perf_counter() - started) * 1000
            rows.append({
                "id": task["id"], "strategy": name,
                "selected_file_count": len(selected),
                "selected_files": selected if name == "local_sha_index" else [],
                "input_tokens_estimate": count,
                "cached_input_tokens_estimate": count if task.get("warm", True) else 0,
                "latency_ms": latency_ms,
                "required_file_recall": len(required & set(selected)) / len(required),
                "stale_context_error": index["sha"] != sha,
            })
    baseline = summarize(baseline_rows, 0.0, 0)
    local = summarize(local_rows, overhead_ms, index_path.stat().st_size)
    reduction = 1 - local["median_input_tokens_estimate"] / baseline["median_input_tokens_estimate"]
    safe = local["required_file_recall"] == 1 and local["stale_context_errors"] == 0
    report = {
        "schema": SCHEMA, "issue": 150, "repository": "aliezzat4321/hyperliquid-copy-engine",
        "sha": sha, "workload": str(workload_path.relative_to(root)),
        "measurement": {
            "token_method": "ceil(utf8_bytes/4); estimated, not provider billing",
            "cached_input_method": (
                "reusable exact-SHA warm context estimate; provider cache counters unavailable"
            ),
            "latency_method": "local context file-read wall clock via time.perf_counter",
        },
        "strategies": {
            "baseline": baseline,
            "graphify": unavailable_graphify(),
            "local_sha_index": local,
        },
        "warm_median_token_reduction": reduction,
        "threshold": .40,
        "decision": "DO_NOT_KEEP_ADDED_LAYER",
        "decision_reason": (
            "local candidate passes the token/recall/staleness gates, but the required Graphify "
            "comparison is unavailable and provider-observed token/cache metrics were not measured"
            if reduction >= .40 and safe else
            "local candidate did not pass the >=40% token reduction plus correctness gates"
        ),
        "fail_open": True,
        "contains_secrets": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workload", type=Path, default=Path("config/repo_context_benchmark.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--index-dir", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    workload = args.workload if args.workload.is_absolute() else root / args.workload
    index_dir = args.index_dir or Path(tempfile.mkdtemp(prefix="repo-context-index-"))
    report = run(root, workload, args.output, index_dir)
    print(json.dumps({"decision": report["decision"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
