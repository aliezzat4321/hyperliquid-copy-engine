# Trader forensics evidence contract

`hlcopy profile-traders` profiles the top rows of the official Hyperliquid leaderboard before the delayed-copy simulator decides whether their trades are executable by a follower.

The profile is intentionally descriptive and provenance-aware. It does **not** claim that a profitable leader is copyable and it does not convert uncertain historical state into fake exact values.

## Profile dimensions

Each trader dossier includes:

- success: win/loss/breakeven rates, expectancy, profit factor, payoff ratio and streaks;
- tail risk: largest loss, worst episode return, drawdown and loss concentration;
- profit quality: top-trade profit concentration and recent-20-trade behavior;
- style: scalper/intraday/swing/position/hybrid, hold-time distribution and frequency;
- directional behavior: long/short mix and PnL;
- position behavior: scale-ins, reductions, reversals and adverse averaging;
- sizing behavior: observed entry notionals and size after losses;
- execution: maker/taker mix, TWAP use and recent order-type/TIF behavior;
- funding: paid/received funding and materiality relative to reconstructed trading PnL;
- concentration: asset count, top-asset share and HHI;
- concurrency: maximum reconstructed simultaneous positions;
- current risk state: configured leverage, cross/isolated mode, margin use and liquidation distance;
- sampled historical effective exposure: position notional versus preceding sampled portfolio equity;
- source coverage and truncation warnings.

## Leverage provenance

Leverage must be interpreted according to its evidence field.

### Exact current configured leverage

Current open-position leverage comes from `clearinghouseState`. The profile records configured leverage value, cross/isolated type, margin used, liquidation price, max leverage and current position value.

Evidence label:

```text
EXACT_CURRENT_CLEARINGHOUSE_STATE
```

This describes the account now. It must not be projected backward over historical trades.

### Historical effective exposure estimate

The first historical approximation uses reconstructed peak position notional divided by the most recent preceding sampled account value from the portfolio history, subject to a maximum sample-age guard.

Evidence label:

```text
SAMPLED_PORTFOLIO_ESTIMATE
```

This is useful for detecting clearly aggressive exposure but it is **not** the historical configured leverage setting. Portfolio graphs are sampled, and account value can change between samples.

### Exact historical configured leverage

Exact historical configured leverage is intentionally not guessed in v1. The next archive-ingestion step will reconstruct `updateLeverage` actions from Hyperliquid L1 `replica_cmds` and join those state changes to fills.

Until then the field is:

```text
PENDING_REPLICA_CMDS_RECONSTRUCTION
```

## API-window warnings

Hyperliquid user history endpoints have finite recent-history windows. The profiler records warning flags when observed evidence reaches those caps or when reconstruction begins from a non-flat position.

Important warnings include:

```text
HISTORY_TRUNCATED
HISTORICAL_ORDER_API_TRUNCATED
TWAP_API_TRUNCATED
LOW_HISTORICAL_EXPOSURE_COVERAGE
```

These are data-quality warnings, not trader-behavior judgments.

## Behavioral warnings

Examples include:

```text
FAST_ALPHA
MAKER_HEAVY
TWAP_HEAVY
CURRENT_HIGH_CONFIGURED_LEVERAGE
HIGH_EFFECTIVE_EXPOSURE_ESTIMATE
FAT_TAIL_LOSSES
PROFIT_CONCENTRATED_IN_FEW_TRADES
SIZE_UP_AFTER_LOSS
AVERAGING_INTO_ADVERSE_MOVES
ASSET_CONCENTRATED
FUNDING_MATERIAL
```

A warning does not automatically reject a wallet. It tells the delayed-copy stage what must be stress-tested.

## Output

A profiling run writes the same evidence in three forms:

```text
outputs/trader_profiles_TIMESTAMP.json
outputs/trader_profiles_TIMESTAMP.csv
outputs/trader_profiles_TIMESTAMP.parquet
```

JSON preserves the nested dossier. CSV/Parquet flatten the major dimensions so traders can be compared and ranked programmatically.

The same profile JSON is persisted in PostgreSQL with model version and as-of/lookback timestamps.

## What comes next

The profile is a prerequisite for, not a replacement for, execution-realistic copyability testing. The delayed-copy backtester will combine the leader's historical actions with historical/recorded market state and simulate follower execution at multiple latency and capital scenarios.

The final decision variable is follower net PnL and edge retention after realistic execution—not leader win rate, leaderboard PnL or configured leverage alone.
