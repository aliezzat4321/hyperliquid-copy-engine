# Issue #150 repository-context benchmark

## Decision

Do not retain Graphify or the simple local index. The local index cleared the 40% token-
reduction floor but failed required-file recall (77.78% versus the required 100%). Graphify
was not installed in the acceptance runner, so a valid three-way comparison was not
possible. An unavailable candidate is not treated as a pass and no substitute graph
implementation is labelled Graphify.

The machine-readable evidence is
[`experiments/issue-150-repo-context-benchmark.json`](experiments/issue-150-repo-context-benchmark.json).
It is keyed to repository SHA `0c8821330779018532c8ee89ace49926b7d47e5a`.

## Method

The reproducible harness `scripts/repo_context_benchmark.py` runs three representative
AI-control-plane retrieval tasks. The baseline supplies every tracked UTF-8 text file.
The simple candidate builds a disposable SHA-keyed token/symbol map and supplies its 12
highest-scoring files. Each task declares required files before retrieval. A SHA mismatch
fails closed instead of serving stale context.

The harness records median/p90 input-token estimate, cached-input estimate, retrieval
latency, required-file recall, stale-context errors, build latency and index bytes. Token
figures are deterministic lexical/index-size proxies, not Claude billing telemetry; that
limitation is explicit in the JSON. Git remains canonical, the index is temporary, and
file contents are not serialized into it.

## Results

| Candidate | Median / p90 input estimate | Cached estimate | Median / p90 latency | Recall | Stale errors | Overhead |
|---|---:|---:|---:|---:|---:|---:|
| Full tracked-text baseline | 525,877 / 525,877 | 0 | 0 / 0 ms | 100% | 0 | 2,531,182 context bytes |
| Simple SHA index | 78,108 / 87,775 | 75,111 | 0.366 / 0.406 ms | 77.78% | 0 | 300,444 bytes; 1,036.162 ms build |
| Graphify | unavailable | unavailable | unavailable | unavailable | unavailable | CLI not installed |

The local median reduction is 85.15%. It is rejected because reduction alone cannot
override the correctness gate. Cached-input figures model the warm serialized index at
four bytes per token; they are not observed provider cache hits.

## Reproduction and completion blocker

Run:

```console
python3 scripts/repo_context_benchmark.py \
  --output docs/ai-team/experiments/issue-150-repo-context-benchmark.json
```

The runner now always writes durable JSON even when Graphify is unavailable, fixing the
original no-evidence failure mode. Completing an actual Graphify comparison requires an
acceptance environment with a reviewed Graphify CLI installation. Until then, the safe
decision is no added context layer.
