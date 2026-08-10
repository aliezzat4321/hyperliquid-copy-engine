# Research -> Validation -> Trading architecture

The system intentionally separates candidate discovery from prospective validation and future live
execution.

## 1. Research

Research may discover candidates from public Hyperliquid data, imported research exports, or other
permitted sources. A candidate enters the registry with `stage=research`. Research code cannot place
orders and a research candidate is not subscribed by the validation wallet collector.

Candidate selection should be point-in-time: store when the wallet was discovered, what data was
available then, and what score/rules caused it to enter research. Do not rebuild candidate lists later
from future leaderboard information and call the result historical performance.

## 2. Validation / shadow

A human or explicit promotion step moves a candidate to `stage=validation`. Hyperliquid wallet
candidates must declare explicit market coins before validation so the system can guarantee L2
coverage. The shadow service then:

- subscribes to public Hyperliquid `userFills` for enabled Hyperliquid wallet candidates;
- records exchange fill timestamp and local receipt timestamp using wall and monotonic clocks;
- records observed exchange-to-local signal latency;
- concurrently captures BBO, L2, trades, and active asset context for covered coins;
- persists spread, depth, imbalance, microprice, funding and mark-price evidence in the market tape;
- persists raw prospective evidence before any strategy decision is made;
- creates an immutable run manifest/fingerprint for reproducibility;
- never places orders.

Registry edits are hot-reloaded. Wallet subscriptions change without a deployment. If validation coin
coverage changes, the market-data supervisor restarts only its child collector with the new universe;
the wallet-fill collector stays alive. The change itself is written as a system event in the evidence
log.

Feed latency and future order latency are kept separate. The shadow recorder measures source-event to
local-receipt latency now. Future paper/live order instrumentation must separately measure decision,
outbound order, exchange acknowledgement and fill latency; production values are never guessed.

## 3. Validation decision

A validation report may say that a wallet is `ELIGIBLE_FOR_HUMAN_APPROVAL`, but the evaluator cannot
promote the wallet itself. Eligibility requires prospective evidence rather than historical headline
ROI and can require minimum sample/days, executable-fill fraction, positive post-cost expectancy,
uncertainty lower bound, latency stress survival, data quality, funding and liquidation-path treatment.

This separation prevents research optimization from silently turning itself into a production rule.

## 4. Approved for trading

`stage=approved` means the wallet passed the validation process and was explicitly promoted. It does
not itself enable real trading. The future trading process must pass two independent gates:

1. the source is enabled and `stage=approved` in the registry;
2. `REAL_TRADING_ENABLED=YES` is explicitly set for the trading process.

The validation systemd service hard-sets `REAL_TRADING_ENABLED=NO`, and the shadow CLI refuses to run
when that variable is `YES`. Future order execution code must call the trading permission boundary
before any state-changing exchange action. Trading code must not be able to change registry stages.

## Registry source types

`hyperliquid_wallet` is a public 42-character Hyperliquid address and can be prospectively subscribed.
`external` is research metadata only. It exists so a source such as an imported trader profile can be
tracked without automating private or prohibited endpoints.

## Promotion principle

Promotion is based on prospective **follower** results, not source headline returns. Validation should
include net return after fees/slippage, separate feed/order latency, missed-fill rate, drawdown/tail
loss, liquidation behavior, funding, regime stability, correlation, data gaps and out-of-sample
persistence. When many candidate wallets are screened, the research process must also account for
multiple-testing/winner's-curse risk.
