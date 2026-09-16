# Invo Notification Executor

Standalone low-latency execution lane for `hyperliquid-copy-engine`.

It consumes **verified Invo trade signals directly from Invo's authenticated API** and mirrors eligible opens/closes into Hyperliquid shadow. It does **not** identify, resolve, or require the source trader's Hyperliquid wallet.

This stays independent from the two existing tracks:

1. Hyperliquid leaderboard / tradable-wallet research.
2. Invo user -> Hyperliquid wallet identification.

## Flow

```text
Invo push (optional wake) ─┐
                          ├─> /v1_0/posts/get_feed
1s API poll fallback ─────┘
                                   |
                         verifiedTrade === true
                                   |
                  exact portfolioId elite gate
                  (pre-trade candidate evidence)
                            /              \
                    rejected            admitted
                       |                    |
               explicit reason      causal HL l2Book
                                            |
                              source close / reconciliation
                                            |
                           net copied economics + latency ledger
```

## Discovery vs shadow admission

Discovery deliberately stays broad. The independent portfolio-research timer continuously
collects broad Invo discovery plus the exact CROWN, 1D, 1W, 1M, 1Y and AT ranked
portfolio surfaces. Those observations are research inputs; **leaderboard membership by
itself is never permission to shadow a trade**.

New dry-run `open` and `increase` signals are admitted only when the signal's exact
`portfolioId` was already classified `ELITE_CANDIDATE` by the persisted portfolio
candidate ledger using evidence available before the source trade. Candidate state that
is missing, malformed, stale, future-dated, absent for that portfolio, or non-elite fails
closed with an explicit skip reason. The same owner cannot transfer eligibility from one
portfolio to another.

The current frozen selector version is `invo-portfolio-elite-v1-20260916`. Its current
quality gate requires, using pre-trade information:

- at least 100 closed positions;
- at least 90 active days when portfolio age is known;
- win rate at least 80%;
- historical displayed return / `percentChange` at least 500%;
- win/loss ratio at least 3;
- not liquidated.

These thresholds are frozen for the current prospective cohort. They must not be tuned
from later shadow outcomes. Future selector versions may incorporate stronger causal
features such as cross-horizon leaderboard persistence, drawdown/tail loss, concentration,
latency and execution capacity, but only prospectively after review.

Demotion blocks new exposure and adds. It never blocks a close: already-managed positions
remain owned, marked and closeable until fully reconciled. This prevents a portfolio from
being removed from the candidate set and silently orphaning its existing exposure.

A notification is only a **wake-up hint**. Notification text can never directly place an
order; every candidate is hydrated from authenticated Invo API data first.

## Discovery and assessment population

The service still observes the authenticated `following`, `all`, and `trending` feed
surfaces for discovery and lifecycle evidence. The broad trader registry is diagnostic;
it no longer authorizes new dry-run exposure. Portfolio-level candidate admission is the
shadow execution boundary.

The legacy trader assessment-entry rule remains useful for research diagnostics: by
default a trader needs at least 20 canonical events collected across 7 distinct
observation days. Source timestamps are retained for freshness, but old posts seen
together at startup count as one observation day. Traders become stale after 3 unseen
days and inactive after 14.

`GET /health` exposes the admission mode, candidate-state freshness policy, aggregate
funnel, every unresolved paper position and its current execution-realistic mark.
`GET /traders` exposes the broad discovery registry.

## Default gates

The deployed service starts dry:

- `REAL_TRADING_ENABLED=NO`
- `NOTIFICATION_TRADER_LIVE=false`
- new/add shadow exposure: exact pre-trade `ELITE_CANDIDATE` portfolio only
- candidate state older than 20 minutes fails closed by default
- research source-age window defaults to 25s
- target margin: 1% of account equity (or paper equity in walletless shadow mode)
- shadow books older than **1000ms** are rejected by default
- the 750ms -> 1000ms changeover is canonically recorded at deploy run `34992024173`, effective `2026-09-15T15:59:18Z`, commit `90ee58d679a093c86543e09a6afc6e3238bc3a74`
- returned Hyperliquid L2 book coin identity is mandatory and must match the requested coin
- shadow spread above 50 bps is rejected by default
- shadow minimum executed notional is $10 by default
- shadow taker-fee assumption is explicit/configurable (4.5 bps per executed side by default)
- no close unless this service recorded ownership of that source trade
- no duplicate entry for an already-managed source trade
- live execution requires **both** `NOTIFICATION_TRADER_LIVE=true` and repo-wide `REAL_TRADING_ENABLED=YES`

## Shadow profitability loop

Dry mode is an execution-realistic trade-lifecycle ledger, not a mid-price entry log:

1. For each hydrated open/increase, read the decision-time portfolio candidate state. Non-elite signals become explicit rejects; only a portfolio proven elite before the source trade may proceed.
2. Fetch the Hyperliquid `l2Book` after the decision, require the returned coin identity, reject missing/stale/wide/zero-depth books, round size down to the asset's `szDecimals`, enforce minimum notional, and walk actual displayed depth.
3. Persist average executed price, book/request/arrival timestamps, spread/book-walk slippage, executed notional, entry taker fee, source size/leverage, copy size and exposure checkpoints.
4. Each feed surface has a durable high-water cursor. Restart and normal polling page backward from newest to the saved cursor. If bounded pagination cannot reach it, the service emits `unrecoverable_feed_gap`, keeps unresolved exposure, and refuses to advance the checkpoint.
5. Owned source closes are reconciled even when late. A close rejected because the requested residual itself is truly below lot/min-notional may be quarantined as `INCOMPLETE_DUST_RECONCILIATION`. If the requested residual is executable but the causal book contains too little displayed depth, it remains `UNRESOLVED_EXPOSURE` and retries with bounded exponential backoff. Thin depth is never reclassified as terminal dust merely because the walked fill is below $10.
6. Complete close economics emit gross PnL, entry/exit taker fees and Hyperliquid funding computed from prospective oracle checkpoints. Missing/stale funding evidence is incomplete, never silently zero.
7. `/health` marks every still-open paper position through executable L2 depth. Closed-only profitability is explicitly forbidden; unresolved exposure remains visible.

Funding evidence is prospective: each hourly funding interval must have a fresh Hyperliquid
`oraclePx` checkpoint; cost is position size × oracle price × funding rate. Missing
checkpoints make economics incomplete rather than substituting entry/mark prices.

The current execution evidence contract is `lane3-causal-l2-v2`; the cost model is
`hl-taker-l2-oracle-funding-v2`; the shadow admission contract is
`lane3-elite-admission-v1-20260916`. These assumptions are evidence inputs for research;
they do not change or authorize the live order route.

Every decision writes JSONL with signal provenance, admission provenance,
detection/decision/market-data latency, sizing, book provenance, cost fields and reason
codes. Source headline PnL is irrelevant if the edge disappears after our latency, fill
drift, fees and funding.

## Credentials

The service reuses the existing repo contract from `/etc/hyperliquid-copy-engine/invo.env`:

- `INVO_ACCESS_TOKEN` or `INVO_REFRESH_TOKEN`
- `WALLET_ADDRESS` and `HL_AGENT_KEY` are required for live execution
- dry mode may run walletless using `NOTIFICATION_TRADER_DRY_EQUITY_USD` for sizing

Service-specific controls live in `/etc/hyperliquid-copy-engine/invo-notification-executor.env`.

Never commit real tokens or private keys.

## Check locally

```bash
cd services/invo-notification-executor
npm install --ignore-scripts --no-audit --no-fund
npm run check
```

## Health

```bash
curl -s http://127.0.0.1:8787/health
curl -s http://127.0.0.1:8787/traders
```

## Optional push wake

```bash
curl -sS -X POST http://127.0.0.1:8787/invo-notification \
  -H 'content-type: application/json' \
  -H "x-bridge-token: $NOTIFICATION_BRIDGE_TOKEN" \
  -d '{"packageName":"com.involio.app","title":"@bones opened SOL"}'
```

That POST only wakes canonical API hydration.

## Promotion criterion

Promotion/demotion is portfolio-level and prospective. Candidate discovery can stay broad,
but only a frozen selector may authorize new elite shadow exposure. Rank candidate
configurations by **our** prospective execution-realistic copied results: realized + MTM
net PnL, fees, funding, causal L2 slippage, fill/reject rate, latency, profit factor,
drawdown and concentration. No real-trading promotion is implied or authorized by this
service.
