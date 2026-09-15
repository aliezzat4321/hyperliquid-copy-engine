# Invo Notification Executor

Standalone low-latency execution lane for `hyperliquid-copy-engine`.

It consumes **verified Invo trade signals directly from Invo's authenticated API** and mirrors eligible opens/closes on our Hyperliquid account. It does **not** identify, resolve, or require the source trader's Hyperliquid wallet.

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
                       dedupe + freshness
                                   |
                    shadow: causal HL l2Book
                    live: guarded HL IOC path
                                   |
                 source close / reconciliation
                                   |
             net copied economics + latency ledger
```

## Discovery and assessment population

Shadow mode rotates through the authenticated `following`, `all`, and `trending`
feed surfaces. It stores a durable, source-provenanced trader registry independently
of whether a Hyperliquid wallet is known. Invo owner ID is canonical; portfolio and
username aliases are retained and merged when a stronger identity appears.

The assessment-entry rule is frozen before economics are inspected: by default a
trader needs at least 20 canonical events collected across 7 distinct observation
days. Source timestamps are retained for freshness, but old posts seen together at
startup count as one observation day. No PnL field participates in queue admission.
Traders become stale after 3 unseen days and inactive after 14; only active traders
can remain in the shadow-assessment queue, allowing newly observed candidates to
replace dead ones automatically. The registry freezes `assessmentEligibleAtMs` at
first qualification; profitability work must use only subsequent shadow evidence
rather than back-selecting earlier PnL.

`GET /health` exposes the aggregate funnel plus every unresolved paper position and
its current execution-realistic mark. `GET /traders` adds each trader's identity
aliases, source surfaces, event count, symbols, freshness, observation days,
lifecycle, and missing/failed reasons.

A notification is only a **wake-up hint**. Notification text can never directly place
an order; every candidate is hydrated from authenticated Invo API data first.

## Default gates

The deployed service starts dry:

- `REAL_TRADING_ENABLED=NO`
- `NOTIFICATION_TRADER_LIVE=false`
- explicit trader allowlist required for live mode unless `COPY_ALL_FOLLOWED=true` is deliberately enabled
- research source-age window is configurable and defaults to 25s
- target margin: 1% of account equity (or paper equity in walletless shadow mode)
- shadow books older than 750ms are rejected by default. This remains aligned with
  `config/lane3_measurement.json`: the 2026-09-15 audit measured 11% of sampled transport
  latency above 750ms but did not establish a safe higher percentile, so the repair removes
  metadata/fan-out delay from book age rather than weakening the causal freshness ceiling.
- shadow spread above 50 bps is rejected by default
- shadow minimum executed notional is $10 by default
- shadow taker-fee assumption is explicit/configurable (4.5 bps per executed side by default)
- live maximum adverse entry chase: 25 bps
- live maximum notional: $500
- live IOC slippage envelope: 0.5%
- live maximum managed positions: 5
- no close unless this service recorded ownership of that source trade
- no duplicate entry for an already-managed source trade
- no entry over an existing unmanaged same-coin live position
- live execution requires **both** `NOTIFICATION_TRADER_LIVE=true` and repo-wide `REAL_TRADING_ENABLED=YES`

## Shadow profitability loop

Dry mode is an execution-realistic trade-lifecycle ledger, not a mid-price entry log:

1. At an eligible Invo open or increase, fetch the Hyperliquid `l2Book` after the decision, reject missing/stale/wide/zero-depth books, round size down to the asset's `szDecimals`, enforce minimum notional, and walk actual displayed depth. Partial fills preserve their unfilled remainder instead of fabricating a full fill.
2. Persist the average executed entry price, book/request/arrival timestamps and age, spread/book-walk slippage, executed notional, entry taker fee, source size/leverage, copy size, and exposure checkpoints. Legacy positions without causal entry-book provenance remain explicitly `INCOMPLETE_LEGACY_ENTRY`.
3. Each feed surface has a durable high-water cursor. Restart and normal polling page backward from newest to the saved cursor. If bounded pagination cannot reach it, the service emits `unrecoverable_feed_gap`, keeps unresolved exposure, and refuses to advance the checkpoint.
4. Owned source closes are reconciled even when late. Close time comes from close/update provenance, not the original open timestamp. The shadow close walks the causal L2 book. A rejected or partial close does **not** erase the remaining position; its close intent is persisted and retried with bounded fan-out and capped backoff across restarts.
5. Complete close economics emit gross PnL, entry/exit taker fees, and Hyperliquid funding computed as position size × prospective `oraclePx` × funding rate at each hourly interval. Funding history is queried from the exact first captured boundary, and query bounds, returned row times, and raw rows are retained in the funding cache. Missing/stale evidence is `INCOMPLETE_FUNDING`, never silently zero or reconstructed from entry/mark prices.
6. `/health` marks eligible still-open paper positions through executable L2 depth with bounded concurrency. Closed-only profitability is explicitly forbidden; unresolved exposure remains visible but is excluded from mark/profitability aggregates until its close resolves.

Funding evidence is prospective: each hourly funding interval must have a fresh Hyperliquid `oraclePx` checkpoint; cost is position size × oracle price × funding rate. Missing checkpoints make economics incomplete rather than substituting entry/mark prices.

The current execution evidence contract is `lane3-causal-l2-v2`; the cost model is
`hl-taker-l2-oracle-funding-v2`. Shadow configuration is explicit in
`.env.example`. These assumptions are evidence inputs for research; they do not
change or authorize the live order route.

Every decision writes JSONL with signal provenance, detection/decision/market-data
latency, sizing, book provenance, cost fields and reason codes. That audit stream is
the profitability dataset: source headline PnL is irrelevant if the edge disappears
after our latency, fill drift, fees and funding.

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

Run shadow first and rank traders by **prospective execution-realistic copied results**: signal count, median/p95 detection latency, stale/book/depth rejection rate, unresolved exposure, and net copied return after measured/declared fees, L2 walk and funding. The configured observation minimum remains 7 days and 20 events per trader before assessment. No real-trading promotion is implied or authorized by this service.
