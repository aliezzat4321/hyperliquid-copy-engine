# Promotion Policy

Machine-readable form: `docs/ai-team/promotion_policy.json`.

**Current version: `quant-promotion-policy-v2` — status `PROVISIONAL`.**

Version 2 adds the deterministic `risk-governor-v1` capital-eligibility ceiling. It
does not alter the v1 profitability floors and does not authorize real trading.

## Why this exists

Both agents must gate on the *same* numbers. If Claude promotes at 30 trades and
Codex promotes at 10, "independent review" degrades into two different standards and
the stricter one simply loses whichever argument happens next.

## What this policy is not

These thresholds are **not** validated truth. They are a starting point chosen to be
conservative in the direction that costs us opportunity rather than capital, on a
lane that has produced 49 closed shadow trades in total. They were first written as
defaults inside one implementation; that is a reason to review them, not a reason to
adopt them.

Nothing here has been fitted to observed results, and nothing here should be treated
as evidence-derived until the review trigger below has been worked.

## Version 1 floors

| Threshold | Value | Rationale | Confidence |
|---|---:|---|---|
| `min_closed_trades` | 30 | Below ~30 the bootstrap interval is so wide that almost nothing can clear a cost floor; this is a practical minimum for the interval to mean anything, not a power calculation. | Low — no power analysis performed |
| `min_distinct_days` | 5 | A slice validated inside one session or one regime is a regime observation, not an edge. | Low — arbitrary but directionally necessary |
| `confidence_level` | 0.90 | Two-sided 90% percentile bootstrap. Chosen over 95% because at n≈30 a 95% bound rejects nearly everything; this is an explicit opportunity/risk trade. | Medium |
| `require_lower_bound_above_cost` | true | The **lower** bound must clear cost. A point estimate above zero is winner's curse when many slices are screened. | High — standard practice |
| `reference_round_trip_cost_bps` | 15 | Two Hyperliquid base-tier taker fees (9 bps) plus a modest spread allowance. **Assumed, not measured** — Lane 3 records no book depth. | Low — must become a measurement |
| `max_profit_concentration` | 0.50 | Blocks a slice whose profit is carried by one trade. | Low — arbitrary |
| `max_unresolved_share` | 0.35 | Unclosed positions never emit a close, so a slice that is mostly open is biased upward. | Low — should probably scale with mean hold time |

## Risk eligibility (version 2)

`src/hlcopy/risk/governor.py` consumes the `risk_governor` object from the machine
policy. Credible edge is only one required input. The evaluator independently checks
audited evidence, drawdown, profit and loss concentration, leverage/margin,
correlated exposure, decision-time liquidity and capacity, latency/staleness,
rejections, tail loss/adverse excursion, unresolved/open/holding exposure, costs,
sample size, days and uncertainty.

The output states are `NO_CAPITAL`, `MICRO_CANDIDATE`, `SMALL_CANDIDATE`, and
`SCALE_CANDIDATE`. They are eligibility ceilings, not capital grants. Shadow evidence
can reach only `MICRO_CANDIDATE`; progressively higher states require validated live
evidence and realized costs. Missing or malformed inputs map to `NO_CAPITAL`, and
deterioration emits `DEMOTE` or `HALT`. Actual capital remains exclusively subject to
`LIVE_TRADING_GATE.md` and explicit owner authorization.

Numeric risk limits are in `promotion_policy.json`. Like profitability thresholds,
changing them requires a new policy version and independent review.

## Known weaknesses

Recorded so the next agent does not have to rediscover them:

1. **No multiple-testing correction.** Scoring many slices at 90% confidence produces
   false positives at a rate proportional to the number of slices. The sample and
   concentration floors only partially compensate.
2. **The bootstrap assumes independent trades.** Copy trades cluster by trader, by
   day and by regime. An i.i.d. bootstrap understates the interval; a clustered or
   block bootstrap is probably correct.
3. **`reference_round_trip_cost_bps` is an assumption.** Until book depth is recorded
   at decision time, any result using it is *execution-cost-scenario shadow edge*,
   not net edge, per `PROFITABILITY_STANDARD.md`.
4. **Post-outcome slicing.** Hold-time and leverage buckets are diagnostics. They must
   not define a promotable entry slice without an ex-ante predictor.

## Changing this policy

Thresholds are versioned. Changing one requires:

1. a new `policy_version` in `promotion_policy.json`, with `supersedes` set;
2. a dated entry in `DECISIONS.md` stating the evidence that justified the change;
3. independent review by the other agent;
4. re-evaluation of any slice previously promoted under the old version.

A threshold must never be loosened to make a specific candidate pass. If a candidate
fails, the finding is that the candidate failed.

## Binding the implementation

Any code that gates promotion must read its thresholds from
`promotion_policy.json` and record the `policy_version` alongside its verdict, so a
result can be traced to the rules that produced it. The Lane 3 engine arriving in
PR #95 currently hard-codes equivalent defaults; binding it to this file is a
follow-up required before any slice is proposed for micro-live.

## Review trigger

First run of the net-edge engine over a complete lane ledger, or 60 days, whichever
comes first.
