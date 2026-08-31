## Objective

## GitHub Issue
Closes #

## Lane / subsystem

## Builder / independent reviewer
Builder:  
Reviewer:  

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
- [ ] `docs/ai-team/state.json` updated if current status/facts/priorities changed
- [ ] experiment record added/updated if this PR makes a material quant claim
- [ ] `DECISIONS.md` updated if architecture/policy changed
- [ ] subsystem docs updated if interfaces/architecture changed
- [ ] no durable-state change required (explain why)

## Live trading
- [ ] No real-trading permission, key, order-route or safety-threshold change
- [ ] If there is such a change, explicit user authorization is linked and scope is documented

## Reviewer challenge
Reviewer should actively try to falsify the claimed improvement: check look-ahead, survivorship bias, multiple testing, PnL/cost math, execution mismatch, data integrity, capacity and unsafe live changes.
