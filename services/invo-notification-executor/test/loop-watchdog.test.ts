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

test('watchdog armed with only a feed limit never reports a direct-watch stall', () => {
  const watchdog = new LoopProgressWatchdog({ feed: 1_000 }, 0);
  // A loop that was never armed (live mode never runs directWatchLoop) must not be
  // able to stall the process even after an arbitrarily long silence, because it has
  // no status row at all rather than an unfed one.
  const status = watchdog.status(10_000_000);
  assert.equal(status.length, 1);
  assert.equal(status[0].loop, 'feed');
  assert.ok(!status.some(row => row.loop === 'direct_watch'));
  watchdog.beat('feed', 10_000_000);
  assert.equal(watchdog.firstStall(10_000_000), null);
});

test('feed watchdog strictly exceeds max-page backfill after max 429 backoff and auth retry', () => {
  const requestTimeoutMs = 2_000;
  const fullBackfillAfterBackoffMs = 30_000 + 20 * 3 * requestTimeoutMs;
  const limit = feedWatchdogLimitMs({ maxBackoffMs: 30_000, pollMs: 1_000,
    maxPages: 20, requestTimeoutMs, maxRequestsPerPage: 3, processingMarginMs: 15_000 });
  assert.equal(limit, 166_000);
  assert.ok(limit > fullBackfillAfterBackoffMs);
  const watchdog = new LoopProgressWatchdog({ feed: limit, direct_watch: limit + 1 }, 0);
  assert.equal(watchdog.firstStall(limit), null);
  assert.equal(watchdog.firstStall(limit + 1)?.loop, 'feed');
});

test('direct watchdog covers a full 24 OPEN plus 24 CLOSED worst-case scan', () => {
  const limit = directWatchdogLimitMs({ openHydrates: 24, closedHydrates: 24,
    openMaxPages: 3, closedMaxPages: 2, maxAttemptsPerPage: 2,
    concurrency: 16, requestTimeoutMs: 2_000, requestBudgetPerSecond: 12,
    requestBudgetBurst: 32, fixedReserveRequestsPerSecond: 4,
    fixedOverheadMs: 2_000, postScanFlushBudgetMs: 25_000, processingMarginMs: 15_000 });
  assert.equal(limit, 108_000);
  assert.ok(limit < 120_000, 'multi-minute stalls must still be detected');
  const transportOnlyMs = 68_000;
  assert.ok(limit > transportOnlyMs + 25_000,
    'limit must strictly cover the full legitimate post-publication execution window');
  const watchdog = new LoopProgressWatchdog({ feed: 166_000, direct_watch: limit }, 0);
  watchdog.beat('feed', limit);
  assert.equal(watchdog.firstStall(limit), null);
  assert.equal(watchdog.firstStall(limit + 1)?.loop, 'direct_watch');
});
