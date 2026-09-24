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

test('a live topology that arms no watchdog at all can never process.exit(1) mid-order', () => {
  // Live execution places real Hyperliquid orders through SDK exchange calls that carry
  // no timeout bound (see hl-client.ts placeMarketOrder/closePosition/setLeverage), so no
  // per-signal heartbeat budget can soundly cover them. service.ts responds by building
  // watchdogLimits as {} in live mode: no loop is armed, so firstStall() can never fire
  // and kill a process that might be mid-order, no matter how long the silence is.
  const watchdog = new LoopProgressWatchdog({}, 0);
  const status = watchdog.status(Number.MAX_SAFE_INTEGER);
  assert.deepEqual(status, [], 'live topology must arm zero loops');
  assert.equal(watchdog.firstStall(Number.MAX_SAFE_INTEGER), null,
    'an unarmed watchdog must never report a stall, even after unbounded silence');
});

test('feed watchdog bounds max 429 backoff, poll wait, and one page/signal unit of work', () => {
  const requestTimeoutMs = 2_000;
  const limit = feedWatchdogLimitMs({ maxBackoffMs: 30_000, pollMs: 1_000,
    requestTimeoutMs, maxRequestsPerPage: 3, signalProcessingBudgetMs: 5_000,
    processingMarginMs: 15_000 });
  // perPageMs (6_000) < signalProcessingBudgetMs (5_000) is false here, so the page
  // budget (3 * 2_000 = 6_000) wins over the smaller signal budget.
  assert.equal(limit, 30_000 + 1_000 + 6_000 + 15_000);
  const watchdog = new LoopProgressWatchdog({ feed: limit, direct_watch: limit + 1 }, 0);
  assert.equal(watchdog.firstStall(limit), null);
  assert.equal(watchdog.firstStall(limit + 1)?.loop, 'feed');
});

test('feed watchdog limit is driven by whichever of the page or signal budget is larger', () => {
  const limit = feedWatchdogLimitMs({ maxBackoffMs: 30_000, pollMs: 1_000,
    requestTimeoutMs: 2_000, maxRequestsPerPage: 3, signalProcessingBudgetMs: 40_000,
    processingMarginMs: 15_000 });
  assert.equal(limit, 30_000 + 1_000 + 40_000 + 15_000);
});

test('per-page heartbeats let a backlog far beyond any fixed page cap avoid a false stall', () => {
  const requestTimeoutMs = 2_000;
  const limit = feedWatchdogLimitMs({ maxBackoffMs: 30_000, pollMs: 1_000,
    requestTimeoutMs, maxRequestsPerPage: 3, signalProcessingBudgetMs: 5_000,
    processingMarginMs: 15_000 });
  const watchdog = new LoopProgressWatchdog({ feed: limit }, 0);
  let nowMs = 0;
  const pageCount = 500; // far beyond any previously-hardcoded feedMaxPages worst case
  for (let page = 0; page < pageCount; page += 1) {
    nowMs += requestTimeoutMs; // bounded per-page transport time before the next beat
    watchdog.beat('feed', nowMs);
    assert.equal(watchdog.firstStall(nowMs), null, `page ${page} must not trip a false stall`);
  }
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
