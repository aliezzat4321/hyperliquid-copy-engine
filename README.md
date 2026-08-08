# Hyperliquid Copy Engine

Research-first infrastructure for finding Hyperliquid traders whose edge is both **persistent** and **replicable after execution reality**.

The objective is not to copy the highest-PnL leaderboard wallets. The objective is to estimate which future leader position changes a follower can capture after latency, spread, slippage, fees, funding, missed fills, partial fills and independent risk controls.

## Current milestone: Phase 1 + reconstruction foundation

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
- ranked CSV + Parquet research output;
- unit tests and CI for the reconstruction core.

**Not yet implemented:** historical order-book replay, true delayed follower fills, funding allocation to episodes, walk-forward historical universe snapshots, live paper watcher, portfolio allocation, or live execution. The current `copyability_score` is deliberately a proxy and must not be interpreted as executable follower ROI.

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

The command writes timestamped outputs to `outputs/`:

```text
ranked_candidates_YYYYMMDDTHHMMSSZ.csv
ranked_candidates_YYYYMMDDTHHMMSSZ.parquet
```

## Key configuration

```text
HLCOPY_MAX_CANDIDATES=25
HLCOPY_MIN_ACCOUNT_VALUE=10000
HLCOPY_MIN_MONTH_ROI=0
HLCOPY_MIN_MONTH_VOLUME=50000
HLCOPY_HTTP_CONCURRENCY=3
```

Start small. Increase `HLCOPY_MAX_CANDIDATES` only after measuring API budget and data quality.

## Data lineage

```text
Hyperliquid response
  -> raw_api_responses (immutable JSONB)
  -> normalized fills / leaderboard snapshots
  -> reconstructed position_episodes
  -> wallet_metrics
  -> ranked research output
```

Raw responses are never replaced by derived records.

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

The next milestone is **real delayed-copy simulation**, not live trading:

1. collect/replay market state around leader fills;
2. evaluate 0ms / 100ms / 250ms / 500ms / 1s / 2s / 3s / 5s / 10s / 30s;
3. cross actual book depth for follower size;
4. include taker/maker fees and funding;
5. replicate target position state, not blindly copy every raw fill;
6. compute follower net PnL and edge retention;
7. use historical point-in-time leaderboard snapshots for walk-forward selection.

Only after that survives out-of-sample and prospective paper trading should a live execution adapter be added.
