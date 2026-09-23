import assert from 'node:assert/strict';
import test from 'node:test';
import { LoopProgressWatchdog } from '../src/loop-watchdog.js';

test('loop watchdog identifies a bounded direct-watch stall while a healthy feed advances', () => {
  const watchdog = new LoopProgressWatchdog({ feed: 1_000, direct_watch: 2_000 }, 10_000);
  watchdog.beat('feed', 12_500);
  assert.deepEqual(watchdog.firstStall(12_500), {
    loop: 'direct_watch', armedAtMs: 10_000, lastProgressAtMs: 10_000,
    maxSilenceMs: 2_000, silenceMs: 2_500, stalled: true,
  });
  watchdog.beat('direct_watch', 12_500);
  assert.equal(watchdog.firstStall(12_500), null);
});
