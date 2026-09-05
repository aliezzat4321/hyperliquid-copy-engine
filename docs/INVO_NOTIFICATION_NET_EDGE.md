# Lane 3 Net Executable Edge

This subsystem implements frozen contract `LANE3-NET-EDGE-CONTRACT-v1` for Issue
#193. It is an offline measurement path over the append-only Lane 3 audit ledger,
persisted ownership state, captured market tape, and public funding history. It does
not route orders and does not authorize capital.

## Measurement boundary

The existing audit rows contain decision-time mids but no bid/ask or depth. For each
leg, the simulated order-arrival timestamp is frozen as that audit decision/detection
timestamp plus configured follower-submit and transport latency. A leg is `MEASURED`
only from the freshest captured L2 book received at or before that arrival boundary,
within the configured age, when the marketable book walk fills the requested quantity.
The decision mid remains the reference, so price movement through arrival is included
in crossing/chase cost. A snapshot after arrival is future-known and is never eligible.
Missing causal books are `UNMEASURED_NO_BOOK`; insufficient depth is
`CAPACITY_INFEASIBLE`. Neither can produce a `net_pnl_usd` value. The 9/15/25/40 bps
grid is emitted only as `execution_cost_scenario_shadow_edge` sensitivity.

Fees are charged at the configured taker rate on every entry/re-up and exit leg.
Funding uses explicit hourly observations and the leg schedule; a missing expected
hour is `UNMEASURED_FUNDING`, never zero. Signal chase and close lag are non-additive
diagnostics because detection decay is already embedded in our mid-to-mid PnL.

## Reconciliation and causality

The ledger deduplicates closes by source position, detects reprocessed signal keys,
retains unpriced closes as quarantine, and classifies every close not owned by the
service. Both frozen reconciliation identities are hard assertions. Promotable
slices are aggregate and entry-time trader×coin cells only. Outcome fields such as
holding time and add count are accepted only in a distinct diagnostic type.

## Statistical and promotion interpretation

The statistic is net return, clustered by UTC day with a seeded 10,000-draw block
bootstrap. Fewer than ten days fails closed. Joint screened cells use Romano-Wolf
step-down adjustment. The lower confidence bound is compared with zero: configured
execution costs are already inside net, so `reference_round_trip_cost_bps` is not
subtracted again.

All thresholds are read from `docs/ai-team/promotion_policy.json`; contract-level
tightening gates cover day clusters, unresolved notional, weighting-sign agreement,
capacity, and true orphans. Retrospective Phase A always fails the prospective gate,
even if every other input is measured. A freeze records at most five retrospective
cells and a UTC cutoff for a later, identical prospective evaluation.

## Commands

Run the coverage probe first, then the report, then freeze the candidate universe:

```text
hlcopy lane3-net-edge coverage-probe --audit AUDIT --state STATE --output coverage.json
hlcopy lane3-net-edge report --audit AUDIT --state STATE --output report.json
hlcopy lane3-net-edge freeze --audit AUDIT --state STATE --report report.json --output freeze.json
```

The host wrapper is `scripts/lane3_net_edge_report.py`. Replacing the protected
status workflow's inline analyzer with this wrapper remains a separate owner action;
this implementation intentionally does not modify `.github/workflows/**`.
