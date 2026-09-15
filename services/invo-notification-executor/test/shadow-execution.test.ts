import assert from 'node:assert/strict';
import test from 'node:test';
import {
  computePositionEconomics,
  fundingCostUsd,
  normalizeL2Book,
  roundSizeDown,
  simulateL2Fill,
  type ShadowExecutionPolicy,
} from '../src/shadow-execution.js';

const policy: ShadowExecutionPolicy = {
  maxBookAgeMs: 750,
  maxSpreadBps: 100,
  minNotionalUsd: 10,
  takerFeeBps: 4.5,
  fundingOracleMaxDelayMs: 10_000,
};

function book(receivedAtMs = 10_500) {
  return normalizeL2Book(
    {
      coin: 'BTC',
      time: 10_000,
      levels: [
        [
          { px: '99.9', sz: '1' },
          { px: '99.8', sz: '2' },
        ],
        [
          { px: '100.1', sz: '1' },
          { px: '100.2', sz: '2' },
        ],
      ],
    },
    10_450,
    receivedAtMs,
    'BTC',
  );
}

test('walks causal L2 depth and supports partial fills without fabricating size', () => {
  const full = simulateL2Fill(book(), 'buy', 2, 3, policy);
  assert.equal(full.ok, true);
  if (!full.ok) return;
  assert.equal(full.fill.filledSize, 2);
  assert.equal(full.fill.partial, false);
  assert.equal(full.fill.avgPx, 100.15);
  assert.equal(full.fill.levelsConsumed, 2);
  assert.equal(full.fill.feeUsd, 200.3 * 4.5 / 10_000);
  assert.equal(full.fill.bookAgeMs, 500);

  const partial = simulateL2Fill(book(), 'buy', 4, 3, policy);
  assert.equal(partial.ok, true);
  if (!partial.ok) return;
  assert.equal(partial.fill.filledSize, 3);
  assert.equal(partial.fill.unfilledSize, 1);
  assert.equal(partial.fill.partial, true);
});

test('rejects stale, excessive-spread, zero-depth and below-min-notional books explicitly', () => {
  const stale = simulateL2Fill(book(11_000), 'buy', 1, 3, policy);
  assert.deepEqual(stale.ok ? null : stale.reason, 'stale_book');

  const wide = normalizeL2Book(
    { coin: 'X', time: 10_000, levels: [[{ px: 90, sz: 10 }], [{ px: 110, sz: 10 }]] },
    10_100,
    10_100,
  );
  const wideResult = simulateL2Fill(wide, 'buy', 1, 3, policy);
  assert.deepEqual(wideResult.ok ? null : wideResult.reason, 'spread_too_wide');

  const zero = normalizeL2Book(
    { coin: 'X', time: 10_000, levels: [[], [{ px: 100.1, sz: 1 }]] },
    10_100,
    10_100,
  );
  const zeroResult = simulateL2Fill(zero, 'buy', 1, 3, policy);
  assert.deepEqual(zeroResult.ok ? null : zeroResult.reason, 'zero_depth');

  const tiny = simulateL2Fill(book(), 'buy', 0.01, 3, policy);
  assert.deepEqual(tiny.ok ? null : tiny.reason, 'below_min_notional');
});

test('enforces Hyperliquid size decimals by rounding down', () => {
  assert.equal(roundSizeDown(1.23456, 3), 1.234);
  assert.equal(roundSizeDown(0.0009, 3), 0);
  const result = simulateL2Fill(book(), 'buy', 0.0009, 3, policy);
  assert.deepEqual(result.ok ? null : result.reason, 'lot_rounded_to_zero');
});

test('computes funding from position size times prospective oracle price times funding rate', () => {
  const calculated = fundingCostUsd(
    'long',
    [{ atMs: 1_000, size: 1 }],
    [
      { timeMs: 2_000, rate: 0.0001 },
      { timeMs: 3_000, rate: 0.0002 },
    ],
    [
      { fundingTimeMs: 2_000, observedAtMs: 2_250, oraclePx: 100 },
      { fundingTimeMs: 3_000, observedAtMs: 3_400, oraclePx: 110 },
    ],
    1_000,
  );
  assert.equal(calculated.fundingUsd, 0.032);
  assert.equal(calculated.fundingPoints, 2);
  assert.equal(calculated.oraclePointsMatched, 2);
});

test('fails funding accounting instead of substituting stale or missing oracle prices', () => {
  assert.throws(
    () => fundingCostUsd(
      'long',
      [{ atMs: 1_000, size: 1 }],
      [{ timeMs: 2_000, rate: 0.0001 }],
      [{ fundingTimeMs: 2_000, observedAtMs: 4_000, oraclePx: 100 }],
      500,
    ),
    /Missing fresh oracle checkpoint/,
  );
  assert.throws(
    () => fundingCostUsd(
      'long',
      [{ atMs: 1_000, size: 1 }],
      [{ timeMs: 2_000, rate: 0.0001 }],
      [],
      500,
    ),
    /Missing fresh oracle checkpoint/,
  );
});

test('computes explicit funding, fees and net pnl without double-counting book slippage', () => {
  const exit = simulateL2Fill(book(), 'sell', 1, 3, policy);
  assert.equal(exit.ok, true);
  if (!exit.ok) return;

  const funding = fundingCostUsd(
    'long',
    [{ atMs: 1_000, size: 1 }],
    [
      { timeMs: 2_000, rate: 0.0001 },
      { timeMs: 3_000, rate: 0.0002 },
    ],
    [
      { fundingTimeMs: 2_000, observedAtMs: 2_100, oraclePx: 100 },
      { fundingTimeMs: 3_000, observedAtMs: 3_100, oraclePx: 100 },
    ],
    500,
  ).fundingUsd;
  assert.equal(funding, 0.03);

  const economics = computePositionEconomics({
    side: 'long',
    entryAvgPx: 100,
    size: 1,
    entryFeeUsd: 100 * 4.5 / 10_000,
    entryNotionalUsd: 100,
    exitFill: exit.fill,
    fundingUsd: funding,
  });
  assert.equal(economics.grossPnlUsd, -0.09999999999999432);
  assert.equal(economics.entryFeeUsd, 0.045);
  assert.equal(economics.exitFeeUsd, 99.9 * 4.5 / 10_000);
  assert.ok(economics.netPnlUsd < economics.grossPnlUsd);
  assert.equal(economics.netReturnBps, economics.netPnlUsd / 100 * 10_000);
});

test('funding size checkpoints apply re-ups to subsequent funding intervals only', () => {
  const funding = fundingCostUsd(
    'short',
    [
      { atMs: 1_000, size: 1 },
      { atMs: 3_000, size: 2.5 },
    ],
    [
      { timeMs: 2_000, rate: 0.001 },
      { timeMs: 4_000, rate: 0.001 },
    ],
    [
      { fundingTimeMs: 2_000, observedAtMs: 2_100, oraclePx: 100 },
      { fundingTimeMs: 4_000, observedAtMs: 4_100, oraclePx: 100 },
    ],
    500,
  ).fundingUsd;
  assert.equal(funding, -0.35);
});
