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

## Automatic fail-closed conditions
If live/shadow divergence, abnormal slippage, integrity failure, source behavior change, risk-limit breach, or required data feed failure occurs, the strategy must stop or demote according to its approved controls.

## No side-door changes
Infrastructure, research and refactor PRs must not modify live permissions, secrets, signing keys, order routing or safety thresholds unless the Issue explicitly says that is the objective and user approval has been obtained.