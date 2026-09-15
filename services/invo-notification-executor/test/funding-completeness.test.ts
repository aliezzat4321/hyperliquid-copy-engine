import assert from 'node:assert/strict';
import test from 'node:test';
import { alignFundingHistoryToCapturedBoundaries } from '../src/funding-alignment.js';
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

test('delayed Hyperliquid funding timestamp aligns to one captured nominal boundary', () => {
  const history = alignFundingHistoryToCapturedBoundaries(
    [{ timeMs: 3_600_117, rate: 0.001 }],
    [3_600_000],
  );
  assert.deepEqual(history, [{ timeMs: 3_600_000, rate: 0.001 }]);
  assert.deepEqual(
    fundingCostUsd(
      'long',
      [{ atMs: 1_000, size: 2 }],
      history,
      [{ fundingTimeMs: 3_600_000, observedAtMs: 3_600_100, oraclePx: 100 }],
      500,
    ),
    { fundingUsd: 0.2, fundingPoints: 1, oraclePointsMatched: 1, maxOracleDelayMs: 500 },
  );
});

test('duplicate funding rows that collapse onto one boundary fail closed', () => {
  assert.throws(
    () => alignFundingHistoryToCapturedBoundaries(
      [
        { timeMs: 3_600_005, rate: 0.001 },
        { timeMs: 3_600_117, rate: 0.001 },
      ],
      [3_600_000],
    ),
    /Duplicate funding-history rows for captured oracle interval 3600000/,
  );
});

test('funding row equidistant from two captured boundaries fails as ambiguous', () => {
  assert.throws(
    () => alignFundingHistoryToCapturedBoundaries(
      [{ timeMs: 2_000, rate: 0.001 }],
      [1_000, 3_000],
      1_000,
    ),
    /Ambiguous funding-history boundary alignment for row 2000/,
  );
});

test('funding row outside tolerance is not silently snapped to a captured boundary', () => {
  const history = alignFundingHistoryToCapturedBoundaries(
    [{ timeMs: 3_602_000, rate: 0.001 }],
    [3_600_000],
  );
  assert.deepEqual(history, [{ timeMs: 3_602_000, rate: 0.001 }]);
  assert.throws(
    () => fundingCostUsd(
      'long',
      [{ atMs: 1_000, size: 1 }],
      history,
      [{ fundingTimeMs: 3_600_000, observedAtMs: 3_600_100, oraclePx: 100 }],
      500,
    ),
    /Missing funding-history row for captured oracle interval 3600000|Missing fresh oracle checkpoint for funding interval 3602000/,
  );
});
