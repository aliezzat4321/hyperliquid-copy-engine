# Streaming path truth v4

## Why this exists

Production evidence on 2026-08-16 showed that the materialized continuous path-risk engine was operationally too expensive for the selective-shadow loop. One mature scenario (`0x1081e214bd6f`, `LIVE_1000MS`, `$1000`) contained 1,118,363 active mark ticks and took about 738 seconds for one exact candidate evaluation. There were 92 mature scenario groups in that cycle. The selective timer was therefore paused before it could fall permanently behind.

## V4 design

V4 does **not** sample marks and does **not** relax any promotion gate. Every supplied mark tick remains part of the risk path.

The old implementation materialized an `EquityCheckpoint` object for each valid mark tick, retained the full path in memory, then traversed that path again for each candidate leverage. V4 instead:

1. streams state changes, funding events and every mark in causal timestamp order;
2. maintains current position, unrealized PnL, gross notional and maintenance margin incrementally;
3. preserves round-trip entry-fee allocation already embedded in follower state;
4. preserves stale/missing mark, funding and margin fail-closed blockers;
5. preserves exchange margin-tier leverage caps;
6. computes peak gross, free-collateral and liquidation-buffer extrema without retaining checkpoint objects;
7. performs one additional bounded-memory pass for exact drawdown percentages after starting equity is known;
8. returns the same `CandidatePathTruth.to_dict()` contract used by promotion and forward-veto code.

The materialized implementation remains available as `evaluate_candidate_path_truth_exact` exclusively as a reference/equivalence oracle.

## Acceptance gate

Do not restart `hyperliquid-selective-shadow.timer` merely because unit tests pass.

Before production rollout:

- Ruff and the full pytest suite must pass.
- Streaming and materialized reference results must match on deterministic equivalence fixtures.
- The paused production million-mark candidate must be run once with both implementations on the same immutable inputs.
- Coverage blockers, checkpoint count, applied funding count, safe-leverage rows, max safe leverage and champion/blocked verdict must match.
- Streaming runtime and peak RSS must be recorded.
- `REAL_TRADING_ENABLED=NO` remains mandatory.

Only after equivalence and a practical production runtime are demonstrated should the selective-shadow timer be re-enabled.
