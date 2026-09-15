import assert from 'node:assert/strict';
import test from 'node:test';
import { mapBounded } from '../src/bounded.js';

test('bounded mapper preserves order and never exceeds its concurrency ceiling', async () => {
  let active = 0;
  let peak = 0;
  const result = await mapBounded([1, 2, 3, 4, 5, 6], 2, async value => {
    active += 1;
    peak = Math.max(peak, active);
    await new Promise(resolve => setTimeout(resolve, 5));
    active -= 1;
    return value * 10;
  });
  assert.deepEqual(result, [10, 20, 30, 40, 50, 60]);
  assert.equal(peak, 2);
});
