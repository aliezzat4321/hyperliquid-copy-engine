# Hyperliquid Copy Engine

Research-first infrastructure for finding Hyperliquid traders whose edge is both **persistent** and **replicable after execution reality**.

The objective is not to copy the highest-PnL leaderboard wallets. The objective is to determine how top traders actually make money, what risk/leverage/execution style that requires, and how much of that edge a follower can retain after latency, spread, slippage, fees, funding, missed fills, partial fills and independent risk controls.

## Current milestone: trader forensics + market evidence

The system now provides the foundation needed before execution-realistic historical copying:

- official Hyperliquid leaderboard discovery;
- top-trader forensic profiling before copyability filtering;
- normalized fill persistence and strict position reconstruction;
- success, tail-risk, consistency and profit-concentration metrics;
- scalper/intraday/swing/position/hybrid style classification;
- long/short, scale-in, reduction, reversal and adverse-averaging behavior;
- maker/taker, TWAP and recent order-style analysis;
- funding dependence and asset concentration;
- current exact leverage/margin/liquidation-state evidence;
- sampled historical effective-exposure estimates with explicit provenance;
- immutable raw API storage in PostgreSQL;
- executable order-book VWAP/slippage primitives;
- continuous Hyperliquid BBO/L2/trades/asset-context capture into immutable Parquet;
- exchange + local nanosecond receive timestamps and explicit reconnect/gap events;
- microprice, spread, BBO imbalance and 5/10 bps depth features;
- unit tests and CI for the research core.

**Not yet implemented:** exact historical configured-leverage reconstruction from L1 actions, historical delayed-copy replay, true follower fills, walk-forward universe selection, live paper watcher, portfolio allocation, or live execution. Existing behavioral copyability scores are proxies and must not be interpreted as executable follower ROI.

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
```

## Profile the top leaderboard traders

Run the forensic profiler before deciding which wallets deserve a realistic copy backtest:

```bash
hlcopy profile-traders
```

Defaults profile the top 20 official leaderboard rows over a 90-day requested lookback. Override them directly:

```bash
hlcopy profile-traders --limit 50 --lookback-days 120
```

Each run writes:

```text
outputs/trader_profiles_YYYYMMDDTHHMMSSZ.json
outputs/trader_profiles_YYYYMMDDTHHMMSSZ.csv
outputs/trader_profiles_YYYYMMDDTHHMMSSZ.parquet
```

The JSON is the nested trader dossier. CSV/Parquet flatten the major fields for screening, comparison and the future delayed-copy engine.

The profiler examines success rate, profit factor, expectancy, payoff ratio, drawdowns, tail losses, profit concentration, recent performance, holding style, direction bias, scaling/reversals, post-loss sizing, maker/taker/TWAP behavior, funding, asset concentration, concurrent positions and leverage/risk evidence.

### Leverage evidence is deliberately provenance-aware

Current open-position configured leverage, cross/isolated mode, margin use and liquidation state come from current clearinghouse state and are labeled exact-current evidence.

Historical effective exposure is currently an estimate using reconstructed position notional versus the preceding sampled portfolio account value. It is explicitly labeled as sampled evidence and is **not** treated as exact historical configured leverage.

Exact historical configured leverage will be reconstructed separately from historical L1 `updateLeverage` actions before that field is used as exact backtest evidence.

See [`docs/trader_forensics.md`](docs/trader_forensics.md) for the full evidence contract and warning definitions.

## Run the original research ranking

```bash
hlcopy pipeline
```

The ranking command writes timestamped candidate CSV/Parquet outputs. It is useful for research prioritization, but its current copyability dimension remains a behavioral proxy rather than follower ROI.

## Start the market tape immediately

Hyperliquid publishes historical market archives, but they can have gaps and they do not contain our local receive timestamps. Our own market collection should therefore run in parallel with wallet research:

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

The collector records connection-loss/reconnect intervals in a `system` partition. Future short-latency simulations must reject periods crossing those gaps rather than inventing missing microstructure.

See [`docs/market_tape.md`](docs/market_tape.md) for the tape contract and replay rules.

## Key configuration

```text
HLCOPY_MAX_CANDIDATES=25
HLCOPY_MIN_ACCOUNT_VALUE=10000
HLCOPY_MIN_MONTH_ROI=0
HLCOPY_MIN_MONTH_VOLUME=50000
HLCOPY_HTTP_CONCURRENCY=3
HLCOPY_PROFILE_CANDIDATES=20
HLCOPY_PROFILE_LOOKBACK_DAYS=90

HLCOPY_MARKET_DATA_DIR=data/market
HLCOPY_MARKET_COINS=BTC,ETH,SOL
HLCOPY_MARKET_FLUSH_ROWS=5000
HLCOPY_MARKET_FLUSH_SECONDS=5
HLCOPY_MARKET_QUEUE_SIZE=50000
```

## Data lineage

```text
Official leaderboard
  -> top leaderboard wallets
  -> raw user fills / funding / orders / account state
  -> strict reconstructed position episodes
  -> trader forensic profiles
  -> execution-realistic delayed-copy replay (next)
  -> follower net PnL / edge retention

Hyperliquid WebSocket / historical archive
  -> market evidence
  -> BBO + L2 + trades + context
  -> follower execution model
```

Raw source records are never replaced by derived records.

## Reconstruction invariant

Every Hyperliquid perp fill contains `startPosition`. During replay, the state machine verifies that its own reconstructed quantity exactly matches this value before applying each fill. A mismatch is treated as a data-quality failure rather than silently producing fake PnL.

Because recent fill APIs can truncate old history, the first visible fill may start with a non-zero position. In that case the engine bootstraps the quantity from `startPosition`, marks that episode `complete_start = false`, and excludes it from scored trade statistics. Once the position returns to flat, subsequent episodes are complete and scoreable.

## Next highest-ROI milestone

After the forensic profiles are validated, the next milestone is **historical execution-realistic copyability simulation**:

1. reconstruct exact historical configured leverage where L1 evidence is available;
2. join leader actions to historical/recorded BBO + L2 state;
3. evaluate 0ms / 100ms / 250ms / 500ms / 1s / 2s / 3s / 5s / 10s / 30s delays;
4. calculate follower VWAP using actual available depth;
5. reject stale or missing market-state intervals;
6. include follower fees, funding, partial fills and missed fills;
7. replicate target position state rather than blindly copying each raw fill;
8. test multiple follower capital sizes;
9. calculate follower net PnL, drawdown and edge retention per trader;
10. only historically robust survivors proceed to prospective live shadow testing.

Real-money execution remains gated behind both historical out-of-sample evidence and prospective paper validation.
