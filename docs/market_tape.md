# Hyperliquid market tape

The market tape exists to answer one question accurately:

> After a leader fill became observable, what market state could the follower actually trade against at a given delay?

Historical candles cannot answer that. The collector therefore records the live microstructure needed by the delayed-copy simulator while the wallet research system is being built.

## Command

```bash
hlcopy capture-market
```

Override the configured universe when needed:

```bash
hlcopy capture-market --coins BTC ETH SOL HYPE
```

The default universe comes from `HLCOPY_MARKET_COINS` and is intentionally small at first. Expand it after measuring disk growth and determining which instruments appear in shortlisted wallet activity.

## Streams

For every configured coin the collector subscribes to:

- `bbo` — best bid/offer changes;
- `l2Book` — full available L2 snapshot messages;
- `trades` — public aggressor-side trades;
- `activeAssetCtx` — mark, oracle, funding, open interest and volume context.

BBO and L2 are both retained. BBO is useful for faster top-of-book state changes while L2 provides executable depth for follower VWAP/slippage simulation.

## Timestamps

Every normalized record stores:

- `exchange_ts_ms` when Hyperliquid provides one;
- `received_at_ns` from the local UTC wall clock;
- `received_monotonic_ns` from the local monotonic clock;
- `observed_event_lag_ms` as a diagnostic difference between exchange and local wall time.

`observed_event_lag_ms` is not trustworthy as absolute network latency unless the capture host has disciplined time synchronization. Run NTP/chrony on the capture host and monitor clock health.

## Storage layout

Tape files are immutable Parquet parts partitioned by UTC receive date, coin and channel:

```text
data/market/
  date=2026-08-08/
    coin=BTC/
      channel=bbo/
        part-....parquet
      channel=l2Book/
        part-....parquet
      channel=trades/
        part-....parquet
      channel=activeAssetCtx/
        part-....parquet
    coin=_ALL/
      channel=system/
        part-....parquet
```

Existing Parquet files are never modified. A flush is first written to a temporary file and then atomically renamed into place.

## Raw + derived fields

Each market row retains `raw_json` in addition to normalized columns. This gives us a path to re-normalize later without pretending today's feature schema is permanent.

The collector also computes inexpensive microstructure features at capture time:

- midpoint;
- spread in basis points;
- BBO size imbalance;
- microprice;
- bid/ask USD depth inside 5 bps and 10 bps;
- depth imbalance at 5 bps and 10 bps;
- trade notional and signed aggressor notional.

Hyperliquid trade side follows exchange notation: `B` is an aggressive buy and `A` is an aggressive sell.

## Disconnect and gap policy

The collector sends Hyperliquid's JSON heartbeat, reconnects with capped exponential backoff plus jitter, and resubscribes all streams after reconnect.

It also writes `system` records for:

- connection open;
- subscription acknowledgements;
- connection loss;
- reconnect waits;
- fatal collector errors.

A future replay must treat intervals between `connection_lost` and the next `connection_open` as data-quality gaps. We must not synthesize 100-500 ms execution evidence across a period in which the capture process did not observe the market.

Public trade duplicates caused by reconnect/replay are suppressed with Hyperliquid's documented unique key of `(block_time, coin, tid)` within a bounded in-memory window. Raw BBO/L2 snapshots are preserved because repeated snapshots remain useful for deterministic replay and gap analysis.

## Backpressure and durability

Network receive and disk writes are decoupled by a bounded async queue. If storage becomes slower than the feed, the queue applies backpressure rather than silently dropping market events.

Files flush on both a row threshold and a time threshold. Process shutdown flushes remaining buffered records. A disk/write failure is treated as fatal rather than allowing the collector to keep running while pretending data is being stored.

## Replay contract

The delayed-copy simulator should consume the tape by event time while retaining receive time. At a leader event timestamp plus latency scenario `L`, it should determine the last market state that would have been observable to our follower, cross the available book for follower quantity, and reject simulations that overlap recorded connection gaps.

Initial latency scenarios:

```text
0 ms theoretical
100 ms
250 ms
500 ms
1 s
2 s
3 s
5 s
10 s
30 s
```

The tape is evidence collection. It does not by itself imply copy trading is profitable.

## Storage lifecycle and pressure policy

`scripts/storage_retention_audit.py` is the fail-closed dependency classifier. It keeps
the recent window and robust evidence at full fidelity, marks older useful evidence for
compression/downsampling, and permits deletion only for old partitions absent from the
complete profitability protection set. `scripts/storage_retention_apply.py` accepts only
a fresh, exact-SHA reviewed manifest and targets the Issue #120 exit band below 80%.

`config/storage_policy.json` is the owner/retention/budget registry for material writers.
`scripts/storage_controller.py` records dataset bytes, positive bytes/hour growth,
aggregate time-to-full, budget breaches and hysteretic pressure decisions. It emits
decisions rather than controlling services; protected manager wiring must stop all listed
writers on pressure and may resume only after a later `ALLOW` decision.

## Historical lifecycle

`scripts/market_tape_lifecycle.py` is the reviewed lifecycle path for historical tape.
It excludes the configured recent window (never less than three days), plans immutable
source identities, and requires the exact manifest SHA-256 before apply. It merges old
channel partitions using zstd level 19 and verifies row counts and reader-required
columns before removing any source file. For `l2Book`, `raw_json` is removed only when
every value is byte-exactly reconstructible from the retained coin, timestamp, bid and
ask columns. Otherwise the column is retained.

This lifecycle is lossless. Lossy time bucketing or depth truncation is deferred to a
separate experiment because it could weaken execution-realistic replay evidence.
