# Experiment Index

Generated from `docs/ai-team/experiments/registry.json`. Do not hand-edit.

Check this index before proposing a hypothesis. Failed and inconclusive results are recorded here precisely so they are not silently repeated.

**Updated:** 2026-08-31T11:56:01Z

| ID | Lane | Status | Evidence | Result | Issue | PR | Builder | Reviewer | Reviewed commit |
|---|---|---|---|---|---:|---:|---|---|---|
| EXP-001 | lane_3 | IN_REVIEW | EXPLORATORY | INCONCLUSIVE | #91 | #95 | CLAUDE | CODEX_CHATGPT | — |

## EXP-001 — lane_3

**Hypothesis:** The Lane 3 gross mid-to-mid shadow PnL survives realistic round-trip execution cost, and its edge is not concentrated in a subset of coins.

**Slice:** All Invo notification shadow trades; sub-sliced by coin (BTC vs alt-coin).

**Retest condition:** Rerun on the full 49-close ledger rather than the 10 rows visible in probe run 33376459723, with measured book spread instead of the 9/15/25/40 bps scenario grid, and a clustered bootstrap that accounts for per-trader and per-day dependence.
