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
                       dry-run / Hyperliquid IOC
                                   |
                    signed position-delta verify
                                   |
                    persistent ownership + audit
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
- target margin: 1% of account equity
- maximum notional: $500
- IOC slippage envelope: 0.5%
- maximum managed positions: 5
- position increases are fail-closed in the MVP
- no close unless this service recorded ownership of that source trade
- no entry over an existing unmanaged same-coin position
- live execution requires **both** `NOTIFICATION_TRADER_LIVE=true` and repo-wide `REAL_TRADING_ENABLED=YES`

Every decision writes JSONL with detection/decision/execution latency, chase, sizing and reason codes. That audit stream is the profitability dataset: source headline P&L is irrelevant if the edge disappears after our latency, fill drift and fees.

## Credentials

The service reuses the existing repo contract from `/etc/hyperliquid-copy-engine/invo.env`:

- `INVO_ACCESS_TOKEN` or `INVO_REFRESH_TOKEN`
- `WALLET_ADDRESS` is optional when embedded in the Invo JWT
- `HL_AGENT_KEY` is required only for live execution

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

Run dry first and rank traders by **prospective copied results**: signal count, median/p95 detection latency, adverse chase bps, stale/chase rejection rate, theoretical fills net of Hyperliquid fees/builder costs, and copied return at the source exit. Only proven positive copied cohorts should enter the live allowlist.
