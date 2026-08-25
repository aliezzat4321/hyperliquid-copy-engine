# Third-Party Wallet Identifier

The production path is deliberately source-agnostic:

1. A read-only third-party adapter collects immutable, closed-trade evidence.
2. Evidence is normalized to the generic closed-trades contract.
3. Independent trade aliases are canonicalized before the evidence threshold is counted.
4. Ready portfolios are resolved against free SQD finalized Hyperliquid fills.
5. Discovery matches are verified on disjoint, full position lifecycles.
6. Only a unique held-out winner is recorded as `VERIFIED`.

Invo is the first adapter. Carmine and Bones are processed first, followed by the rest of
the ready Invo queue. Adding another third party should require a new read-only collector
that emits the same resolver CSV, not another wallet-matching engine.

## Continuous flow

`hyperliquid-invo-source-miner.timer` runs the read-only collector every five minutes. A
successful collection triggers `hyperliquid-invo-wallet-identifier.service`. The identifier:

- reuses one SQD client and its coverage/header cache across the batch;
- skips unchanged verified evidence and exponentially backs off unchanged unresolved evidence
  from one hour to a 24-hour maximum while still allowing SQD coverage to catch up;
- retries transport/runtime errors on the next successful collector cycle;
- processes at most four changed portfolios per run;
- never promotes a wallet to validation, shadow, or live trading.

Durable outputs are under `/var/lib/hyperliquid-copy-engine/invo`:

- `resolution_queue/resolution_queue.json`: resolver-ready Invo portfolios;
- `identifier_state.json`: per-portfolio attempts and evidence hashes;
- `identified_wallets.json`: verified identity summary;
- `wallet_identifications/<portfolio>/`: immutable resolver reports.

## Bootstrap

Run once on the research VM:

```bash
cd /root/hyperliquid-copy-engine
sudo bash scripts/bootstrap_invo_source_miner.sh
```

The bootstrap validates the Invo credential before enabling the timer and restores the old
credential if replacement authentication fails. The wallet identifier refuses to run when
`REAL_TRADING_ENABLED=YES`.

## Current starting identities

Bones has already passed strict production acceptance as
`0x565590f4d2b00b567a564f56b13f898392aef180`. Carmine remains an Invo-priority target and
will be recorded only when the same strict public-data identity gate returns a unique held-out
winner.
