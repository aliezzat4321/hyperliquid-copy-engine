# Three-Lane Profitability Scoreboard

Generated from `docs/ai-team/profitability_scoreboard.json`. Do not hand-edit.

**As of:** 2026-08-31T12:32:51Z  
**Policy:** `quant-promotion-policy-v1`  
**Real trading:** DISABLED

> Decision priority for producing accepted executable net-edge evidence, not a claim that any lane is profitable. Missing required evidence fails closed.

| Rank | Lane | Gross theoretical edge | Net executable edge | Sample | Copyability / latency | Verdict | Distance |
|---:|---|---|---|---|---|---|---|
| 1 | Direct third-party notification copy | 37.771087 USD; avg 0.770839 USD/close | UNKNOWN: — | 49 closes; 6 unresolved | 57.291667%; median 11032 ms | `FAIL_INSUFFICIENT_EXECUTABLE_NET_EVIDENCE` | NEAR: run the existing net-edge attribution work on the complete ledger, mark unresolved positions, and capture decision-time book spread/depth |
| 2 | Hyperliquid leaderboard selective wallet by asset copy | —; avg — | UNKNOWN: — | — closes; — unresolved | —; median — | `FAIL_MISSING_PUBLISHED_PROSPECTIVE_ECONOMICS` | NEAR_TO_MEDIUM: publish target-level prospective economics and automate causal handoff from the selective cohort funnel |
| 3 | Third-party wallet resolver then Hyperliquid copy | —; avg — | NOT_OBSERVED: — | 0 closes; — unresolved | —; median — | `FAIL_ZERO_VERIFIED_IDENTITIES` | MEDIUM: validate the builder-first resolver and produce at least one identity before matched Lane 2 versus Lane 3 measurement |

## Interpretation

Lane 3 ranks first for research effort because it alone has observed shadow closes and gross dollar PnL, not because executable profitability has been established. Lane 1 ranks second because frozen wallet-by-asset targets exist but their required economics are not in trusted evidence. Lane 2 ranks third because zero verified identities makes an execution comparison impossible.

## 1. Direct third-party notification copy

- Evidence: `EXPLORATORY`; verdict: `FAIL_INSUFFICIENT_EXECUTABLE_NET_EVIDENCE`.
- Gross theoretical: 37.771087 USD PnL; — return; 0.770839 USD average PnL per close. Mid-to-mid gross shadow closes; excludes costs and six unresolved positions. Average PnL is derived as 37.771087 / 49.
- Net executable: UNKNOWN — No accepted fee, spread, impact, funding, and unresolved-position-adjusted result.
- Sample: — eligible signals; 55 opens; 49 closes; 6 unresolved; — distinct days.
- Outcomes/risk: win rate —; payoff ratio —; max drawdown —; downside concentration —.
- Costs/latency/copyability: UNMEASURED; UNMEASURED; policy reference cost is an assumed 15 bps round trip, not an observation; funding UNMEASURED; 55 opens / (55 opens + 41 signals rejected by the 25 second freshness gate); diagnostic only because total eligible signals and other rejection classes are unavailable.
- Stability/confidence: Observed 2026-08-31T09:11:47Z; degradation UNKNOWN; Prospective-like shadow collection, but accepted net attribution is absent and closes are survivorship-biased; NONE; no clustered interval and no accepted complete-ledger analysis.
- Capacity/concentration: UNKNOWN; no decision-time book depth or measured impact.
- Smallest next measurement: Complete-ledger, trader/day-clustered net attribution with measured decision-time book costs and explicit marking of the six unresolved positions.
- Sources: WORKFLOW_RUN `33376459723` observed 2026-08-31T09:11:47Z, EXPERIMENT `EXP-001` observed 2026-08-31T11:56:01Z.

## 2. Hyperliquid leaderboard selective wallet by asset copy

- Evidence: `FROZEN_PROSPECTIVE`; verdict: `FAIL_MISSING_PUBLISHED_PROSPECTIVE_ECONOMICS`.
- Gross theoretical: — PnL; — return; — average PnL per close. The funnel reports candidate counts but no current aggregate economics suitable for this scoreboard.
- Net executable: UNKNOWN — Two targets are labelled approved, but the trusted observation does not publish per-target trades, costs, PnL, drawdown, or uncertainty.
- Sample: — eligible signals; — opens; — closes; — unresolved; — distinct days.
- Outcomes/risk: win rate —; payoff ratio —; max drawdown —; downside concentration —.
- Costs/latency/copyability: NOT_PUBLISHED_IN_TRUSTED_OBSERVATION; NOT_PUBLISHED_IN_TRUSTED_OBSERVATION; funding NOT_PUBLISHED_IN_TRUSTED_OBSERVATION; UNKNOWN.
- Stability/confidence: Observed 2026-08-31T11:40:40Z; degradation UNKNOWN; Four frozen targets exist, but broad-screen selection is disconnected from the frozen queue and one target has zero events; NONE PUBLISHED; broad screening requires multiple-testing control.
- Capacity/concentration: Target notionals are configured, but executable capacity and concentration are not published in trusted evidence.
- Smallest next measurement: For each frozen wallet by coin cohort, publish closed and unresolved counts, distinct days, gross and net PnL in USD, costs, worst-latency execution survival, drawdown, and clustered confidence.
- Sources: WORKFLOW_RUN `33369211976` observed 2026-08-31T07:37:45Z, WORKFLOW_RUN `33388075503` observed 2026-08-31T11:40:40Z.

## 3. Third-party wallet resolver then Hyperliquid copy

- Evidence: `EXPLORATORY`; verdict: `FAIL_ZERO_VERIFIED_IDENTITIES`.
- Gross theoretical: — PnL; — return; — average PnL per close. No verified identity has entered an execution-aware comparison.
- Net executable: NOT_OBSERVED — Zero published verified identities.
- Sample: — eligible signals; — opens; 0 closes; — unresolved; — distinct days.
- Outcomes/risk: win rate —; payoff ratio —; max drawdown —; downside concentration —.
- Costs/latency/copyability: NOT_OBSERVED; NOT_OBSERVED; funding NOT_OBSERVED; Fill copyability is unobserved. Identity publication yield is 0 published identities / 174 resolution-ready portfolios..
- Stability/confidence: Observed 2026-08-31T11:40:40Z; degradation NOT_APPLICABLE_WITH_ZERO_IDENTITIES; No matched-trader prospective execution sample; NONE.
- Capacity/concentration: UNKNOWN until identities are verified and Hyperliquid fills can be paired.
- Smallest next measurement: Publish the first verified identity with resolution latency, then pair that trader's Hyperliquid events to the same notification signals and compare completeness, latency, fills, and net PnL.
- Sources: WORKFLOW_RUN `33388075503` observed 2026-08-31T11:40:40Z.

## Candidate work order

1. **Lane 3 complete-ledger direct notification shadow cohort** — Only lane with observed shadow closes and positive gross dollars; closest to a falsifiable executable-net verdict. Status: `RESEARCH_PRIORITY_NOT_PROMOTED`.
2. **Lane 1 frozen wallet by coin cohorts** — Selectivity is already represented by frozen wallet by coin targets, but the trusted report must publish complete prospective economics before any target can be ranked. Status: `RESEARCH_PRIORITY_NOT_PROMOTED`.
3. **Matched-trader Lane 2 versus Lane 3 pair** — This is the causal comparison the issue requires, but it cannot begin until the resolver publishes a verified identity. Status: `BLOCKED_ON_VERIFIED_IDENTITY`.

## Promotion and demotion

Apply quant-promotion-policy-v1 without modification: at least 30 closed prospective trades across at least 5 distinct days; the two-sided 90% lower confidence bound on net economics must exceed cost; maximum single-trade profit concentration 0.50; maximum unresolved share 0.35. Additionally require measured decision-time execution costs, explicit funding, drawdown, capacity, latency and execution survival, frozen entry-time rules, and correction for broad screening before any future live consideration.

Demote to research when any policy floor fails, net confidence falls to or below cost, unresolved share or concentration breaches its cap, data integrity or execution parity fails, the candidate materially degrades across a new prospective window, or the rule changes and therefore requires a new frozen window.

**Safety:** A promotion verdict does not authorize capital. REAL_TRADING_ENABLED remains false and LIVE_TRADING_GATE still requires explicit, scoped user authorization.

**Decision:** Focus measurement effort on Lane 3 first, Lane 1 target-level prospective publication second, and Lane 2 resolver yield third. No lane is eligible for live consideration.
