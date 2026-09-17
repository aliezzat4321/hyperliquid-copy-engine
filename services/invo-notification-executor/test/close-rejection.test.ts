import assert from 'node:assert/strict';
import test from 'node:test';
import { shouldTerminallyDustReconcile } from '../src/close-rejection.js';

test('executable requested residual with sub-minimum displayed depth is not terminal dust', () => {
  // Requested residual is $100 at the causal mid, so it is executable. A later
  // `below_min_notional` caused by only ~$9 of displayed close-side depth must
  // remain unresolved/retryable rather than deleting managed exposure.
  assert.equal(shouldTerminallyDustReconcile('below_min_notional', 1, 3, 100, 10), false);
});

test('true below-minimum requested residual is terminal dust', () => {
  assert.equal(shouldTerminallyDustReconcile('below_min_notional', 0.05, 3, 100, 10), true);
});

test('sub-lot requested residual is terminal dust', () => {
  assert.equal(shouldTerminallyDustReconcile('lot_rounded_to_zero', 0.0004, 3, 100, 10), true);
});

test('missing causal mid cannot be used to erase exposure as dust', () => {
  assert.equal(shouldTerminallyDustReconcile('below_min_notional', 0.05, 3, null, 10), false);
});
