# Live Trading Gate

## Authority
Only the user can authorize moving real capital. An AI agent, CI result, shadow report, PR approval or experiment result cannot authorize live trading by itself.

## Default
`REAL_TRADING_ENABLED=NO` and all equivalent service-level live flags remain disabled.

## Before requesting micro-live authorization
A candidate must have:
- a frozen prospective definition;
- execution-cost-adjusted shadow evidence satisfying `PROFITABILITY_STANDARD.md`;
- no unresolved material data-integrity defects;
- documented position sizing and maximum loss;
- documented entry/exit execution method;
- circuit breaker and automatic demotion path;
- rollback/kill procedure;
- independent AI review of profitability and execution assumptions;
- explicit statement of the exact capital/notional requested.

## Authorization scope
Any approval is specific to the named candidate, maximum notional/capital, service and stage. It is not permission to enable unrelated strategies or increase allocation.

## Structured authorization record

Authorization is recorded in `docs/ai-team/state.json` as a complete object. Free text
is not accepted. `scripts/ai_team_contract.py` rejects `authorized: true` unless every
field below is present and valid, and CI runs that validator on every pull request.

```json
"live_trading": {
  "authorized": true,
  "status": "AUTHORIZED",
  "authorization": {
    "authorized_by": "USER",
    "scope": {
      "lane": "lane_3",
      "slice": "<exact promotable slice>",
      "service": "<service that will place orders>",
      "stage": "MICRO_LIVE",
      "max_notional_usd": 50
    },
    "authorized_at": "<RFC3339>",
    "approval_reference": "LIVE-AUTH-YYYY-MM-DD-NNN",
    "expires_at": "<RFC3339, after authorized_at and in the future>",
    "revoked": false
  }
}
```

Enforced invariants:

- `authorized_by` must be `USER`. No agent, CI result, review or experiment can occupy
  this field.
- `approval_reference` must match `LIVE-AUTH-\d{4}-\d{2}-\d{2}-\d{3}`. A free-text
  justification is not a reference.
- `expires_at` must be after `authorized_at` **and** in the future. An expired
  authorization fails CI rather than lingering as an implied permission.
- `revoked: true` is incompatible with `authorized: true`.
- `authorized: false` requires `authorization: null`, so a lapsed grant cannot be left
  in place to be silently reactivated by flipping one boolean.
- `stage` and `max_notional_usd` are part of the grant. Increasing either is a new
  authorization, not an edit.

**An agent must never create, infer, extend or reactivate this object.** It is written
only when the user has explicitly authorized that exact scope.

## Live-sensitive change guard

`scripts/check_live_sensitive_change.py`, run by `.github/workflows/live-sensitive-guard.yml`,
detects pull requests that touch real-trading permissions, order routing, signing/key
handling, live systemd environment or safety thresholds, and fails unless the PR
description declares `LIVE-SENSITIVE: YES`.

The guard **classifies only**. Declaring a change live-sensitive makes it visible for
review; it does not authorize trading and never sets the authorization object above.

## Automatic fail-closed conditions
If live/shadow divergence, abnormal slippage, integrity failure, source behavior change, risk-limit breach, or required data feed failure occurs, the strategy must stop or demote according to its approved controls.

## No side-door changes
Infrastructure, research and refactor PRs must not modify live permissions, secrets, signing keys, order routing or safety thresholds unless the Issue explicitly says that is the objective and user approval has been obtained.
