## Objective

## GitHub Issue
Closes #

## Lane / subsystem

## Builder / independent reviewer
Builder (logical agent: CLAUDE / CODEX_CHATGPT):  
Reviewer (logical agent: CLAUDE / CODEX_CHATGPT):  
Reviewed commit SHA:  

GitHub cannot prove these apart — both agents act through one account. See
`docs/ai-team/REVIEW_PROVENANCE.md`. Record them anyway; a stale or self-review is
only detectable if the reviewed SHA is written down.

## Hypothesis / problem

## Before

## After

## Profitability impact
State the evidence level from `docs/ai-team/PROFITABILITY_STANDARD.md`. Do not use "profitable" without the required economics/evidence.

## Data and evaluation window
- sample size:
- distinct days:
- exploratory / frozen prospective / shadow / live:
- unresolved/open outcomes:

## Execution assumptions / measurements
- fees:
- spread/slippage/impact:
- funding:
- latency:
- capacity/notional:

## Statistical validity
- confidence method:
- multiple-testing treatment:
- causal leakage checks:

## Tests / validation

## Production impact / rollback

## Durable-state impact
- [ ] `docs/ai-team/state.json` updated if current status/facts/priorities changed (every fact needs `observed_at` and a source reference)
- [ ] `docs/ai-team/experiments/registry.json` updated if this PR makes a material quant claim, and `INDEX.md` regenerated
- [ ] `DECISIONS.md` updated if architecture/policy changed
- [ ] subsystem docs updated if interfaces/architecture changed
- [ ] no durable-state change required (explain why)

## Live trading
- [ ] No real-trading permission, key, order-route or safety-threshold change
- [ ] If there is such a change, explicit user authorization is linked and scope is documented

If this PR touches real-trading permissions, order routing, signing/key handling,
live systemd environment or safety thresholds, add the line below. The
`live-sensitive-guard` check requires it and will fail without it. Classification is
not authorization.

<!-- LIVE-SENSITIVE: YES -->

## Reviewer challenge
Reviewer should actively try to falsify the claimed improvement: check look-ahead, survivorship bias, multiple testing, PnL/cost math, execution mismatch, data integrity, capacity and unsafe live changes.
