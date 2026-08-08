# Hyperliquid Copy Engine

Research-first infrastructure for finding Hyperliquid traders whose edge is both **persistent** and **replicable after execution reality**.

The objective is not to copy the highest-PnL leaderboard wallets. The objective is to estimate which future leader position changes a follower can capture after latency, spread, slippage, fees, funding, missed fills, partial fills and independent risk controls.

## Current milestone: research foundation + live market evidence capture

This repository intentionally starts before live execution. V0.1 implements the highest-ROI foundation:

- Hyperliquid stats leaderboard discovery;
- cheap leaderboard pre-screening before expensive per-wallet API calls;
- weighted API-rate limiting and retries;
- immutable raw API response storage in PostgreSQL;
- normalized fill persistence with duplicate protection;
- position-episode reconstruction using `startPosition` as a data-quality invariant;
- correct handling of scale-ins, reductions, closes and single-fill reversals;
- explicit detection of truncated fill history so partial historical episodes are not scored;
- fee-aware reconstructed performance metrics;
- transparent behavioral copyability proxy used only for research prioritization;
- executable order-book VWAP/slippage primitives for the next delayed-copy stage;
- continuous Hyperliquid BBO/L2/trades/asset-context capture into immutable Parquet;
- exchange + local nanosecond receive timestamps and explicit reconnect/gap events;
- microprice, spread, BBO imbalance and 5/10 bps depth features;
- ranked CSV + Parquet research output;
- unit tests and CI for the reconstruction and market-data core.

**Not yet implemented:** historical delayed-copy replay, true follower fills, funding allocation to episodes, walk-forward historical universe snapshots, live paper watcher, portfolio allocation, or live execution. The current `copyability_score` is deliberately a proxy and must not be interpreted as executable follower ROI.

## Why the pipeline pre-screens first

Hyperliquid's API uses weighted REST limits and wallet fill history is materially more expensive than fetching the cached leaderboard. Pulling thousands of full wallet histories blindly wastes the request budget and slows research. V0.1 ranks the cheap leaderboard fields first and only downloads recent unaggregated fills for the most promising candidates.

Defaults are deliberately conservative and configurable.

## Quick start

```bash
cp .env.example .env

docker compose up -d postgres

python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

set -a
source .env
set +a

hlcopy pipeline
```

The research command writes timestamped outputs to `outputs/`:

```text
ranked_candidates_YYYYMMDDTHHMMSSZ.csv
ranked_candidates_YYYYMMDDTHHMMSSZ.parquet
```

## Start the market tape immediately

Historical high-resolution BBO/L2 state cannot be recreated reliably after the fact, so market-data collection should run in parallel with wallet research:

```bash
hlcopy capture-market
```

Default markets are `BTC,ETH,SOL`. Override them directly when useful:

```bash
hlcopy capture-market --coins BTC ETH SOL HYPE
```

Data is written as append-only partitioned Parquet:

```text
data/market/date=YYYY-MM-DD/coin=BTC/channel=bbo/part-....parquet
data/market/date=YYYY-MM-DD/coin=BTC/channel=l2Book/part-....parquet
data/market/date=YYYY-MM-DD/coin=BTC/channel=trades/part-....parquet
data/market/date=YYYY-MM-DD/coin=BTC/channel=activeAssetCtx/part-....parquet
```

The collector records connection-loss/reconnect intervals in a `system` partition. Future 100-500 ms simulations must reject periods crossing those gaps rather than inventing missing microstructure.

See [`docs/market_tape.md`](docs/market_tape.md) for the tape contract and replay rules.

## Key configuration

```text
HLCOPY_MAX_CANDIDATES=25
HLCOPY_MIN_ACCOUNT_VALUE=10000
HLCOPY_MIN_MONTH_ROI=0
HLCOPY_MIN_MONTH_VOLUME=50000
HLCOPY_HTTP_CONCURRENCY=3

HLCOPY_MARKET_DATA_DIR=data/market
HLCOPY_MARKET_COINS=BTC,ETH,SOL
HLCOPY_MARKET_FLUSH_ROWS=5000
HLCOPY_MARKET_FLUSH_SECONDS=5
HLCOPY_MARKET_QUEUE_SIZE=50000
```

Start small. Increase `HLCOPY_MAX_CANDIDATES` and the market universe only after measuring API budget, disk growth and data quality.

## Data lineage

```text
Hyperliquid response
  -> raw_api_responses (immutable JSONB)
  -> normalized fills / leaderboard snapshots
  -> reconstructed position_episodes
  -> wallet_metrics
  -> ranked research output

Hyperliquid WebSocket
  -> immutable market Parquet tape
  -> normalized microstructure fields
  -> delayed-copy replay (next milestone)
```

Raw source records are never replaced by derived records.

## Reconstruction invariant

Every Hyperliquid perp fill contains `startPosition`. During replay, the state machine verifies that its own reconstructed quantity exactly matches this value before applying each fill. A mismatch is treated as a data-quality failure rather than silently producing fake PnL.

Because recent fill APIs can truncate old history, the first visible fill may start with a non-zero position. In that case the engine bootstraps the quantity from `startPosition`, marks that episode `complete_start = false`, and excludes it from scored trade statistics. Once the position returns to flat, subsequent episodes are complete and scoreable.

## Ranking philosophy

The current research composite exposes separate dimensions:

- performance;
- risk;
- persistence across leaderboard windows;
- behavioral copyability proxy;
- statistical confidence.

Warnings flag low sample sizes, very fast alpha, maker-heavy behavior, high concentration, and recent reconstructed losses. Win rate is recorded but is not a primary scoring input.

## Next highest-ROI milestone

With market evidence now being collected, the next milestone is **real delayed-copy simulation**, not live trading:

1. replay market state around leader fills;
2. evaluate 0ms / 100ms / 250ms / 500ms / 1s / 2s / 3s / 5s / 10s / 30s;
3. use BBO plus L2 depth to calculate achievable follower VWAP;
4. reject periods with market-tape gaps or stale state;
5. include taker/maker fees and funding;
6. replicate target position state, not blindly copy every raw fill;
7. compute follower net PnL and edge retention;
8. use historical point-in-time leaderboard snapshots for walk-forward selection.

Only after that survives out-of-sample and prospective paper trading should a live execution adapter be added.
