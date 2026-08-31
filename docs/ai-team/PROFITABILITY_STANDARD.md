# Profitability Standard

No agent may describe a strategy, wallet, trader, slice or lane as "profitable" without stating the evidence level and the economics below.

## Required evidence fields
- sample size / completed trades;
- distinct trading days and evaluation window;
- discovery/in-sample vs frozen prospective vs shadow vs live status;
- gross PnL and return;
- fees;
- spread/slippage/market impact (measured where possible; scenario assumptions clearly labelled);
- funding/financing where relevant;
- net or execution-cost-adjusted PnL and return;
- open/unresolved positions and missing outcomes;
- drawdown and loss concentration;
- latency / signal age and execution timing;
- target notional and capacity evidence;
- uncertainty / confidence method;
- selection method and multiple-testing correction where broad screening occurred.

## Evidence ladder
1. **Exploratory candidate** — historical/discovery evidence only.
2. **Frozen prospective candidate** — rule, slice and cutoff fixed before new observations.
3. **Shadow validated** — prospective evidence under execution-realistic simulation.
4. **Micro-live candidate** — shadow evidence cleared the approved gate; still requires explicit capital authorization.
5. **Micro-live validated** — real fills agree with shadow assumptions at tiny notional.
6. **Scaled candidate** — enough live evidence to consider larger allocation.

Never collapse these labels into one generic "profitable" claim.

## Causality
Entry-time rules may use only information available at the decision timestamp. Post-outcome variables such as realized holding time may be diagnostic but cannot directly define a promotable entry slice without an ex-ante predictor.

## Statistics
- Freeze hypotheses before prospective validation.
- Broad searches must account for winner's curse / multiple testing.
- Promotion confidence should be based on net economics, not gross alpha compared with a cost afterward.
- Prefer clustered/block methods when trades share trader/day/regime dependence.
- Report the chosen confidence level and why.
- A point estimate above zero is never sufficient by itself.

## Data integrity
Material malformed rows, accounting mismatches, duplicate attribution, impossible prices/sizes or missing required economics must fail closed for promotion. Integrity defects may be analyzed, but cannot be waved through because PnL is positive.

## Costs
Use measured fee tier, book/spread, slippage and funding whenever available. If scenario costs are used, call the result **execution-cost-scenario shadow edge**, not true net edge.

Report trade-weighted and dollar/notional-weighted metrics separately when sizing differs materially.

## Promotion thresholds
This document defines *what must be reported and how it must be reasoned about*. The
numeric floors a slice must clear are versioned separately in
`PROMOTION_POLICY.md` / `promotion_policy.json`, so both agents gate on the same
numbers and a threshold change is a reviewed decision rather than a silent code edit.

Report the `policy_version` alongside any promotion verdict.

## Live gate
Profitability evidence alone never enables trading. `LIVE_TRADING_GATE.md` governs capital authorization.
