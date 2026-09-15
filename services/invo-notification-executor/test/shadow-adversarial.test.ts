import assert from 'node:assert/strict';
import test from 'node:test';
import { normalizeL2Book, simulateL2Fill, type ShadowExecutionPolicy } from '../src/shadow-execution.js';

const policy: ShadowExecutionPolicy = {
  maxBookAgeMs: 750,
  maxSpreadBps: 50,
  minNotionalUsd: 10,
  takerFeeBps: 4.5,
};

test('missing L2 book is an explicit shadow rejection', () => {
  const result = simulateL2Fill(null, 'buy', 1, 3, policy);
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.reason, 'missing_book');
});

test('future-dated L2 book timestamp is rejected rather than treated as fresh', () => {
  const futureBook = normalizeL2Book(
    {
      coin: 'BTC',
      time: 11_000,
      levels: [[{ px: 99.9, sz: 2 }], [{ px: 100.1, sz: 2 }]],
    },
    10_400,
    10_500,
    'BTC',
  );
  const result = simulateL2Fill(futureBook, 'buy', 1, 3, policy);
  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.equal(result.reason, 'stale_book');
    assert.equal(result.detail?.bookAgeMs, -500);
  }
});
