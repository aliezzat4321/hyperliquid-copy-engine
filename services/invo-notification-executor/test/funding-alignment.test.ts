import assert from 'node:assert/strict';
import test from 'node:test';
import { alignFundingHistoryToCapturedBoundaries } from '../src/funding-alignment.js';

test('aligns a delayed funding row to exactly one captured hourly boundary', () => {
  const result = alignFundingHistoryToCapturedBoundaries(
    [{ timeMs: 3_600_049, rate: 0.0001 }],
    [3_600_000],
  );
  assert.deepEqual(result, [{ timeMs: 3_600_000, rate: 0.0001 }]);
});

test('fails closed when one funding row is within tolerance of multiple captured boundaries', () => {
  assert.throws(
    () => alignFundingHistoryToCapturedBoundaries(
      [{ timeMs: 1_500, rate: 0.0001 }],
      [1_000, 2_000],
      600,
    ),
    /Ambiguous funding-history boundary alignment/,
  );
});

test('fails closed on duplicate rows targeting the same captured boundary', () => {
  assert.throws(
    () => alignFundingHistoryToCapturedBoundaries(
      [
        { timeMs: 1_010, rate: 0.0001 },
        { timeMs: 1_020, rate: 0.0002 },
      ],
      [1_000],
      100,
    ),
    /Duplicate funding-history rows/,
  );
});
