# Experiment Template

**ID:** EXP-###  
**Issue:** #  
**Builder:**  
**Reviewer:**  
**Date:**  
**Lane:**  

## Hypothesis
What causal/economic claim is being tested?

## Frozen definition
What exact slice/rule was fixed before the prospective window? If exploratory only, say so explicitly.

For evaluated work, attach the machine-readable `frozen_contract`, its canonical
SHA-256 `contract_fingerprint`, `evaluations`, and any append-only
`implementation_revisions` defined by `hlcopy.research.experiment_controller`.
The contract must include the hypothesis/rationale, lane and strategy, universe and
entry-time rule/features, parameters/grid, discovery and untouched prospective
windows, measured-vs-assumed costs, notional/capacity, evidence floors, metrics and
statistics/multiple-testing method, pass/fail/abandonment rules, and exact code/data
provenance. The first evaluation locks that fingerprint.

Changing a locked decision requires a new experiment ID/version and a new untouched
window. An implementation repair may retain the fingerprint only when recorded as
`IMPLEMENTATION_REPAIR` with its new commit SHA and all affected evidence is rerun.
Post-outcome diagnostic slices remain exploratory until frozen in a new experiment.

## Data
Window, source, sample size, distinct days, open/unresolved outcomes, exclusions and integrity checks.

## Execution economics
Gross PnL/return, fees, spread/slippage/impact, funding, latency, notional/capacity and whether each cost is measured or assumed.

## Statistics
Confidence method, clustering/blocking, multiple-testing treatment and concentration/drawdown.

## Result
`PASS` / `FAIL` / `INCONCLUSIVE` with evidence level from `PROFITABILITY_STANDARD.md`.

## Decision
What changes because of this result? What must happen before the next stage?

## Retest condition
State exactly what new evidence or changed assumption would justify rerunning a failed/inconclusive experiment.

## Legacy registry migration
Schema-v1 registry summaries remain readable and are classified as legacy exploratory
records. They are not evidence of a frozen contract. Before prospective evaluation or
promotion, create a new version with the machine-readable contract above; never infer
missing freeze timestamps or holdout boundaries from an old result.
