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

## Storage lifecycle

Storage lifecycle decisions are manifest-driven and fail closed. The most recent three
UTC days and every robust candidate coin remain full fidelity. Older tape with positive
screening evidence is classified for compression/downsampling, while an old partition is
eligible for deletion only when the complete screening evidence deterministically proves
that no positive or robust coin dependency exists. `storage_retention_audit.py` creates
the review artifact and `storage_retention_apply.py` accepts only a fresh, exact-SHA
reviewed manifest. PostgreSQL and fills are never filesystem-delete candidates.
The emergency manifest may contain at most 12 GiB of deterministically eligible
partitions. This permits the 75% healthy target to be reachable from the declared
44 GiB dataset-budget envelope while retaining a hard, independently reviewed cap.

The permanent policy is `config/storage_governance_v1.json`. It assigns each material
dataset an owner, retention class, byte budget, growth budget and its material writer
units. `storage_controller.py` records bytes/hour and time-to-full, pauses all governed
writers on absolute, forecast or aggregate-growth pressure, and uses a separate resume
threshold for hysteresis. Deployment wiring is intentionally not changed here; the
manager must install the controller only after exact-SHA independent review.
