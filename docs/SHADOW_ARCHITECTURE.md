# Research -> Validation -> Trading architecture

The system intentionally separates candidate discovery from prospective validation and future live
execution.

## 1. Research

Research may discover candidates from public Hyperliquid data, imported research exports, or other
permitted sources. A candidate enters the registry with `stage=research`. Research code cannot place
orders and a research candidate is not subscribed by the validation wallet collector.

## 2. Validation / shadow

A human or explicit promotion step moves a candidate to `stage=validation`. The shadow service then:

- subscribes to public Hyperliquid `userFills` for enabled Hyperliquid wallet candidates;
- records exchange fill timestamp and local receipt timestamp using wall and monotonic clocks;
- records whether the fill's coin is covered by the configured market-capture universe;
- concurrently captures BBO, L2, trades, and active asset context for covered coins;
- persists raw prospective evidence before any strategy decision is made;
- never places orders.

Registry edits are reloaded by the wallet-fill collector without code changes. Market coin coverage is
computed at process start from validation/approved wallet coin lists plus explicit extra coins. Restart
the shadow service after changing coin coverage.

## 3. Approved for trading

`stage=approved` means the wallet has passed the validation policy. It does not itself enable real
trading. The future trading process must have a separate explicit real-trading gate and must consume
only approved candidates. It must not be able to change registry stages.

## Registry source types

`hyperliquid_wallet` is a public 42-character Hyperliquid address and can be prospectively subscribed.
`external` is research metadata only. It exists so a source such as an imported trader profile can be
tracked without automating private or prohibited endpoints.

## Promotion principle

Promotion must be based on prospective follower results, not source headline returns. Candidate
validation should eventually include minimum sample size, net return after fees/slippage, latency
survival, missed-fill rate, drawdown/tail loss, liquidation behavior, funding, regime stability,
correlation, and out-of-sample persistence.
