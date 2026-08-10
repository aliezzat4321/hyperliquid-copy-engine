# Systematic-trading design choices for the Hyperliquid copy engine

This document records which ideas from `wangzhe3224/awesome-systematic-trading` and its linked
projects are adopted, adapted, or intentionally deferred.

## Adopt now

### Event-driven research -> validation -> trading separation

Use one normalized event model and separate services. Research can discover and rank candidates but
cannot trade. Validation collects prospective evidence and cannot trade. Future trading consumes only
explicitly approved candidates and remains behind a second global `REAL_TRADING_ENABLED=YES` gate.

This follows the useful architectural property seen in event-driven frameworks in the curated list and
the machine-approval boundary described by Inalpha: research logic is not allowed to become an order
side effect merely because an agent recommends it.

### Feed latency and order latency are different quantities

HftBacktest treats feed latency and order latency separately. We do the same. Every source fill stores:

- source exchange timestamp;
- local wall-clock receipt timestamp;
- local monotonic receipt timestamp;
- measured exchange-to-local observation lag.

Future paper/live execution instrumentation must separately measure decision time, outbound order
latency, exchange processing/ack latency, and fill latency. No production default latency is guessed.

### Full L2 evidence, not candles

The market recorder already stores BBO, full available L2 snapshot JSON, trades, spread, 5/10-bps
USD depth, imbalance, microprice, funding, mark price and open interest. Copyability evaluation should
use executable book depth/VWAP and reject stale or insufficient-depth observations.

Microprice and imbalance are execution/context features. They are not assumed to be profitable alpha.

### Deterministic evidence and point-in-time discipline

TraderHarness emphasizes strict historical visibility, deterministic replay and evidence fingerprints.
For prospective shadowing we persist immutable run manifests containing the exact registry snapshot,
coin universe and configuration fingerprint. Raw wallet fills and market evidence are append-only.
Research candidate selection must be timestamped and must not be reconstructed from a future
leaderboard after seeing later performance.

### Realistic fill policy tournament

Immediate taker execution is the first baseline because it is the cleanest copy test. Later execution
policies may include bounded taker, post-only, and patient-then-cross variants. The latter borrows the
important idea from FlashAlpha's fill simulator that a posted limit should not be assumed filled simply
because a theoretical price was touched. Maker policies require queue-aware simulation or prospective
paper/live evidence before they can be preferred over taker execution.

### Prospective promotion gate

A source is never automatically approved from historical ROI. Promotion evidence must be prospective
and include enough completed trades/days, executable-fill rate, post-cost expectancy, uncertainty lower
bound, latency stress, data-gap rate, funding and liquidation-path treatment. Passing the gate only makes
a source eligible for explicit approval; the evaluator cannot mutate the registry stage.

## Adopt when multiple validated wallets exist

### Cost-aware portfolio allocation

The curated list includes cvxportfolio, skfolio and Riskfolio-Lib. Their important idea for us is not
mean-variance optimization on source returns. It is constrained allocation using expected follower
returns, covariance/correlation, transaction costs, leverage/exposure limits and estimation risk.

We should only optimize across prospectively validated follower return streams. Before that, equal-risk
or capped confidence weights are safer than fitting an optimizer to sparse source history.

### Multiple-testing defenses

Scanning many wallets creates a winner's-curse problem. Candidate research should track the number of
wallets/hypotheses screened and use walk-forward / holdout evaluation, bootstrap uncertainty,
multiple-testing correction where a valid p-value exists, and parameter-neighborhood sensitivity.
The candidate selection timestamp and rules must be frozen before the prospective validation window.

## Deferred intentionally

### HftBacktest as a direct dependency

HftBacktest is highly relevant for L2/L3 replay, queue position and feed/order latency. We do not vendor
or add it as a runtime dependency yet because our immediate validation is prospective and taker-based.
Once enough local L2/trade tape has accumulated, an adapter can export our tape to HftBacktest for
queue-aware maker-policy research. This avoids adding a large dependency before it changes a decision.

### AI-generated live orders

No LLM or research agent will receive an exchange-order function. AI may propose code, hypotheses,
candidate wallets or allocation changes; deterministic machine rules and explicit promotion/permission
gates decide what is eligible for execution.
