# Invo notification lane: net edge contract

The notification executor writes an append-only JSONL audit stream. Until now the
only summary of that stream was an inline probe in
`.github/workflows/invo-notification-executor-status.yml` that reported **gross**
mid-to-mid copied PnL over completed trades.

That number cannot support a capital decision, for four independent reasons:

1. **No execution cost.** Entry and exit are both recorded at the Hyperliquid mid
   we saw. A real follower crosses the spread and pays a taker fee on both legs.
2. **Survivorship.** A source position that is still open never emits
   `shadow_closed`, so it never enters the sample. If losers are held longer than
   winners — the usual case — the closed-trade sample is biased upward.
3. **No attribution.** A positive aggregate can be produced by one trader, one
   coin, or one outlier trade while the rest of the followed universe loses money.
4. **Discarded rejections.** Signals refused by the freshness gate are the only
   evidence we have about the upstream publication-latency distribution, and they
   were being thrown away.

`hlcopy.profitability.notification_edge` replaces that probe with a tested engine.

## Reconstruction rules

* An `shadow_opened` / `shadow_opened_from_increase` row starts a trade keyed by
  `sourceBaseId`; `shadow_reupped` updates size, blended entry and notional.
* A `shadow_closed` row for a trade we never opened is an **orphan** and is never
  scored. The audit stream can begin mid-position, and inventing an entry price for
  those rows would manufacture PnL.
* A repeated `shadow_closed` for an already-scored position is a **duplicate** and
  is counted once. Invo emits more than one close post per source position.
* Positions with no close are retained as **open**, not silently dropped, so
  survivorship is measurable rather than invisible.
* PnL is **recomputed** from entry mid, exit mid, size and side. The value the
  executor logged is treated as evidence and any disagreement is reported as
  `pnl_mismatch_rows`. This mirrors the `startPosition` reconstruction invariant
  used elsewhere in the repository.

## Cost handling

Rather than hard-coding a spread guess, every slice reports:

* `breakeven_cost_bps` — the round-trip execution cost at which the slice's mean
  return reaches zero. This is assumption-free: it is a property of the trades.
* `net_by_cost_bps` — net PnL, mean/median net return, net win rate and net
  drawdown at each of several explicit round-trip cost scenarios. The default set
  is 9 / 15 / 25 / 40 bps. `9` is two Hyperliquid base-tier taker fees and nothing
  else — an unreachable floor that assumes a zero-width spread and no impact. The
  wider scenarios are the realistic range for alt-coin books.

Cost is charged half on the entry notional and half on the exit notional, so a
trade that moved is charged on both of its actual leg sizes.

## Promotion gate

`EdgePolicy` requires **all** of the following before a slice is reported
`ELIGIBLE_FOR_MICRO_LIVE`:

| Gate | Default | Why |
| --- | --- | --- |
| `min_closed_trades` | 30 | A handful of trades cannot separate edge from noise. |
| `min_distinct_days` | 5 | Guards against a single session or regime. |
| bootstrap CI lower bound > `reference_cost_bps` | 15 bps | The **lower** bound must clear cost, not the point estimate. |
| `max_profit_concentration` | 0.50 | Blocks a slice carried by one lucky trade. |
| `max_open_trade_share` | 0.35 | Blocks a slice whose sample is mostly unresolved. |

The bootstrap is seeded, so the same ledger always yields the same verdict. A gate
that flickers between runs is not a gate.

Eligibility is a **report**, not an action. Nothing here mutates the shadow
registry, proposes an order, or changes a trading stage, and the CLI refuses to run
under `REAL_TRADING_ENABLED=YES`.

## Slices

Edge is attributed across `trader`, `coin`, `side`, `trader × coin`,
`trader × side`, `trader × coin × side`, signal-age bucket, hold-time bucket and
leverage bucket. A trader is rarely uniformly good; these keys let one profitable
combination be promoted without adopting the rest of that trader's book.

The `signal_age` dimension is the direct test of whether Invo's publication latency
is destroying the copied edge: if mean net return falls monotonically across the
`age_0_2s` → `age_15s_plus` buckets, latency is the binding constraint and a faster
signal surface is worth more than any change to trader selection.

## Two measurements nobody was making

* `exit_vs_source_bps` — the executor records `sourceClosingPrice` on every close
  but never compared it to the mid we actually saw. This is the exit-side analogue
  of entry chase, and the only available measure of how late our exits are.
  `detectionLatencyMs` is unusable on close rows because Invo's close post carries
  the position's original timestamp.
* `stale_signals` — the age distribution of everything the 25-second freshness gate
  refused, with the rejection rate. This quantifies both the upstream latency tail
  and the opportunity cost of the gate's current setting.

## Running it

```bash
python -m hlcopy.profitability.notification_edge_cli \
  --audit /var/lib/hyperliquid-copy-engine/invo-notification-executor/audit.jsonl \
  --output /root/hyperliquid-audit/invo-notification/edge_report.json \
  --reference-cost-bps 15
```

`.github/workflows/invo-notification-edge.yml` runs this read-only on the
self-hosted research runner every six hours.
