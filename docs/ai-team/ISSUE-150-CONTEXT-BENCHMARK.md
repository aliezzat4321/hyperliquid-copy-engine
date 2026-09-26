# Issue #150 repository-context benchmark

Status: `INCOMPLETE_COMPARISON`; decision: `DO_NOT_KEEP_ADDED_LAYER`.

The reproducible machine-readable result is
`docs/ai-team/benchmarks/issue-150-context-benchmark.json`. It was produced by:

```bash
python3 scripts/repo_context_benchmark.py \
  --output docs/ai-team/benchmarks/issue-150-context-benchmark.json
```

## Scope and method

The frozen three-task replay workload is `config/repo_context_benchmark.json`. The
baseline supplies every tracked text file. The local candidate creates a disposable
SHA-keyed path/term/symbol/dependency repository map and returns at most 12 files.
Required files are used only for scoring recall, never injected into selection. Git is canonical, an index SHA
mismatch is a stale-context error, and failure of an added layer leaves the normal Git
workflow available.

Input and cached-input figures are deterministic UTF-8 byte estimates, not Claude
billing counters. Cached input represents the reusable exact-SHA context on a warm
repeat. Latency is local file-read wall time. This checkout has neither a Graphify
executable/pinned adapter nor provider cache telemetry, and no network installation was
attempted.

## Result and decision

The local candidate reduced estimated median warm context from 632,523 to 74,125 tokens
(88.28%). Its p90 was 80,900 versus the 632,523 baseline, required-file recall was
100%, and it produced zero stale-context errors. Building the index took about 164 ms
and 1.06 MB in this checkout. Exact timings and sizes are retained in the JSON report.

These local results clear the issue's 40% reduction floor, but the mandatory Graphify
comparison and provider-observed cached-input measurement were not possible. Therefore
the benchmark does **not** authorize retaining either added layer. A completion runner
must rerun the same workload with a pinned Graphify adapter and provider usage telemetry,
then apply the unchanged >=40% and no-correctness/safety-regression gate.

This benchmark is control-plane-only. It changes no trading, risk, capital, exact-SHA,
CI, protected-path, or merge gate. Real trading remains disabled.
