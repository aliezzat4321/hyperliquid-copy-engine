import assert from 'node:assert/strict';
import test from 'node:test';
import { directWatchdogLimitMs, feedWatchdogLimitMs, LoopProgressWatchdog } from '../src/loop-watchdog.js';

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

test('feed watchdog includes capped 429 backoff plus poll, request, and processing margin', () => {
  const limit = feedWatchdogLimitMs({ maxBackoffMs: 30_000, pollMs: 1_000,
    requestTimeoutMs: 2_000, processingMarginMs: 15_000 });
  assert.equal(limit, 48_000);
  const watchdog = new LoopProgressWatchdog({ feed: limit, direct_watch: 90_000 }, 0);
  assert.equal(watchdog.firstStall(limit), null);
  assert.equal(watchdog.firstStall(limit + 1)?.loop, 'feed');
});

test('direct watchdog covers a full 24 OPEN plus 24 CLOSED worst-case scan', () => {
  const limit = directWatchdogLimitMs({ openHydrates: 24, closedHydrates: 24,
    openMaxPages: 3, closedMaxPages: 2, maxAttemptsPerPage: 2,
    concurrency: 16, requestTimeoutMs: 2_000, requestBudgetPerSecond: 12,
    requestBudgetBurst: 32, fixedReserveRequestsPerSecond: 4,
    fixedOverheadMs: 2_000, processingMarginMs: 15_000 });
  assert.equal(limit, 83_000);
  assert.ok(limit < 120_000, 'multi-minute stalls must still be detected');
  const watchdog = new LoopProgressWatchdog({ feed: 48_000, direct_watch: limit }, 0);
  watchdog.beat('feed', limit);
  assert.equal(watchdog.firstStall(limit), null);
  assert.equal(watchdog.firstStall(limit + 1)?.loop, 'direct_watch');
});
