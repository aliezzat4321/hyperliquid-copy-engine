import assert from 'node:assert/strict';
import test from 'node:test';
import { fundingCostUsd } from '../src/shadow-execution.js';

test('captured funding boundary may not disappear from funding history as optimistic zero', () => {
  assert.throws(
    () => fundingCostUsd(
      'long',
      [{ atMs: 1_000, size: 2 }],
      [],
      [{ fundingTimeMs: 2_000, observedAtMs: 2_100, oraclePx: 100 }],
      500,
    ),
    /Missing funding-history row for captured oracle interval 2000/,
  );
});

test('missing one history row among multiple captured funding boundaries fails closed', () => {
  assert.throws(
    () => fundingCostUsd(
      'long',
      [{ atMs: 1_000, size: 1 }],
      [{ timeMs: 2_000, rate: 0.001 }],
      [
        { fundingTimeMs: 2_000, observedAtMs: 2_100, oraclePx: 100 },
        { fundingTimeMs: 3_000, observedAtMs: 3_100, oraclePx: 101 },
      ],
      500,
    ),
    /Missing funding-history row for captured oracle interval 3000/,
  );
});

test('zero funding is allowed only when no active captured funding boundary is expected', () => {
  assert.deepEqual(
    fundingCostUsd('long', [{ atMs: 1_000, size: 1 }], [], [], 500),
    { fundingUsd: 0, fundingPoints: 0, oraclePointsMatched: 0, maxOracleDelayMs: 500 },
  );
});
