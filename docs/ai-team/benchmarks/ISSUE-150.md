# Issue #150 repository-context benchmark

## Decision

**INCONCLUSIVE — DO NOT KEEP an added context layer.** The local SHA-keyed candidate
reduced median serialized context tokens by 17.41%, below the required 40%, and required
file recall was only 83.33%. Graphify was unavailable in the isolated runner, so no
Graphify metrics can honestly be reported. Absence of that comparison fails closed; it
does not justify retaining either candidate.

The machine-readable evidence is
[`issue-150-repo-context.json`](issue-150-repo-context.json). Reproduce it from the
repository root with:

```bash
python3 scripts/repo_context_benchmark.py --output /tmp/issue-150.json
```

## Method and limitations

Three fixed control-plane review tasks exercise routing, bounded review prompts, and
durable acceptance evidence. Baseline context is the union of the task files plus the
mandated project context. The local candidate builds a disposable symbol/term map,
keys it by `git rev-parse HEAD`, selects at most four files, and rejects a mismatched SHA.

Input/context tokens are deterministic lexical-token counts, not provider-billed tokens.
Cached input reports the selected warm context eligible for reuse; it is not a claim
about a provider cache hit. Latency is local selection latency. The index stores paths
and lexical terms only, no file contents or credentials. GitHub and repository files
remain canonical, and index failure falls back to baseline context.

The benchmark ran at `fcc5fe54175250037cccf8aa6ffc3e42fc207cb9`. Timing values are
environment-specific. A future retest requires a pinned, locally available Graphify
executable and must retain these tasks and gates. Until then, Issue #150's full
baseline-vs-Graphify-vs-local comparison remains blocked.
