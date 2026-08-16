# Selective path-truth performance v3

Production on 2026-08-16 showed that exact path-risk replay could wedge the selective-shadow systemd oneshot even after mark and margin indexing. The remaining cause was doing full continuous MTM/funding/margin/liquidation replay for every scenario group, including candidates with fewer than the 30 realized forward actions required for promotion or forward-veto maturity. Long inactive mark tails also inflated exact replay work.

The v3 runner preserves fail-closed promotion semantics while bounding work:

- candidates below `MIN_FORWARD_ACTIONS` are explicitly deferred with incomplete path truth; they cannot validate or obtain safe leverage;
- mature candidates still receive the complete causal path-risk replay;
- for mature candidates, market marks and funding are restricted to intervals where copied positions are actually open, while retaining boundary mark context;
- the forward shadow continues accumulating state evidence for deferred candidates, so they automatically become eligible for exact path replay after crossing 30 realized actions;
- real trading remains disabled by the systemd unit.

This is a computational eligibility gate, not a relaxation of validation. No candidate can become a validated champion without the same complete path truth, safe leverage, positive worst-latency return, minimum actions, and minimum forward time required previously.
