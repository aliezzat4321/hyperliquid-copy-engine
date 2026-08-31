# Invo Notification Executor

Standalone low-latency execution lane for `hyperliquid-copy-engine`.

It consumes **verified Invo trade signals directly from Invo's authenticated API** and mirrors eligible opens/closes on our Hyperliquid account. It does **not** identify, resolve, or require the source trader's Hyperliquid wallet.

This stays independent from the two existing tracks:

1. Hyperliquid leaderboard / tradable-wallet research.
2. Invo user -> Hyperliquid wallet identification.

## Flow

```text
Invo push (optional wake) ─┐
                          ├─> /v1_0/posts/get_feed (following)
1s API poll fallback ─────┘
                                   |
                         verifiedTrade === true
                                   |
                    dedupe + freshness + chase
                     + trader + leverage gates
                                   |
                       shadow / Hyperliquid IOC
                                   |
             source close -> copied close at our HL mid
                                   |
             copied P&L + latency + persistent ownership
```

A notification is only a **wake-up hint**. Notification text can never directly place an order; every candidate is hydrated from authenticated Invo API data first.

## Default gates

The deployed service starts dry:

- `REAL_TRADING_ENABLED=NO`
- `NOTIFICATION_TRADER_LIVE=false`
- explicit trader allowlist required for live mode unless `COPY_ALL_FOLLOWED=true` is deliberately enabled
- maximum source age: 5s
- maximum adverse entry chase: 25 bps
- maximum leverage: 20x
- target margin: 1% of account equity (or paper equity in walletless shadow mode)
- maximum notional: $500
- IOC slippage envelope: 0.5%
- maximum managed positions: 5
- position increases are fail-closed in the MVP
- no close unless this service recorded ownership of that source trade
- no duplicate entry for an already-managed source trade
- no entry over an existing unmanaged same-coin live position
- live execution requires **both** `NOTIFICATION_TRADER_LIVE=true` and repo-wide `REAL_TRADING_ENABLED=YES`

## Shadow profitability loop

Dry mode is a real trade-lifecycle ledger, not an entry logger:

1. At an eligible Invo open, record the Hyperliquid mid visible to **us at detection time**, simulated size/notional/margin, source trader, chase and latency.
2. Persist that ownership across restarts.
3. When the same source trade closes, record the Hyperliquid mid visible to us at close detection.
4. Emit `shadow_closed` with gross copied P&L, gross return bps, return on simulated margin, holding time, and close-detection latency.
5. A restart recovers matching source closes from the startup feed rather than baselining them away.

This intentionally reports **gross** copied P&L. Fee/builder/funding assumptions should be layered on from measured Hyperliquid execution costs rather than hard-coded guesses.

Every decision writes JSONL with detection/decision/execution latency, chase, sizing and reason codes. That audit stream is the profitability dataset: source headline P&L is irrelevant if the edge disappears after our latency, fill drift and fees.

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

Run shadow first and rank traders by **prospective copied results**: signal count, median/p95 detection latency, adverse chase bps, stale/chase rejection rate, copied gross return at the source exit, and then measured execution/funding/fee costs. Only positive copied cohorts after those costs should enter the live allowlist.

That ranking is produced by `hlcopy.profitability.notification_edge`, which reads this service's audit stream and attributes net-of-cost edge across trader, coin, direction, signal age, hold time and leverage. It reports each slice's breakeven round-trip cost and a bootstrap lower bound, and only marks a slice `ELIGIBLE_FOR_MICRO_LIVE` when that lower bound clears the cost the slice must actually pay. See [`docs/INVO_NOTIFICATION_NET_EDGE.md`](../../docs/INVO_NOTIFICATION_NET_EDGE.md).

The gross figures this service logs are inputs to that engine, not a promotion signal on their own.
