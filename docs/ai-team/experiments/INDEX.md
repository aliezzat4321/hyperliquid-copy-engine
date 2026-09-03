# Experiment Index

Generated from `docs/ai-team/experiments/registry.json`. Do not hand-edit.

Check this index before proposing a hypothesis. Failed and inconclusive results are recorded here precisely so they are not silently repeated.

**Updated:** 2026-09-03T02:48:44Z

| ID | Lane | Status | Evidence | Result | Issue | PR | Builder | Reviewer | Reviewed commit |
|---|---|---|---|---|---:|---:|---|---|---|
| EXP-001 | lane_3 | IN_REVIEW | EXPLORATORY | INCONCLUSIVE | #91 | #95 | CLAUDE | CODEX_CHATGPT | — |
| EXP-002 | lane_1 | IN_REVIEW | EXPLORATORY | FAIL | #177 | #179 | CODEX_CHATGPT | CLAUDE | — |

## EXP-001 — lane_3

**Hypothesis:** The Lane 3 gross mid-to-mid shadow PnL survives realistic round-trip execution cost, and its edge is not concentrated in a subset of coins.

**Slice:** All Invo notification shadow trades; sub-sliced by coin (BTC vs alt-coin).

**Retest condition:** Rerun on the full 49-close ledger rather than the 10 rows visible in probe run 33376459723, with measured book spread instead of the 9/15/25/40 bps scenario grid, and a clustered bootstrap that accounts for per-trader and per-day dependence.

## EXP-002 — lane_1

**Hypothesis:** A causal previous-day high/low liquidity-sweep reversal rule has positive execution-cost-adjusted expectancy on liquid crypto markets during the London morning and survives an untouched holdout.

**Slice:** BTCUSDT and ETHUSDT Binance USD-M 5-minute proxy data; first 08:00-10:00 Europe/London PDH/PDL sweep-reclaim with immediate-next-bar confirmation; frozen 1R/1.5R/2R/3R targets.

**Retest condition:** Do not retune or rerun the frozen rule after seeing the holdout. Open a new experiment only for a materially different preregistered causal hypothesis or new Hyperliquid-specific execution evidence, with a new untouched validation window.
