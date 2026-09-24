import assert from 'node:assert/strict';
import test from 'node:test';
import {
  INVO_PRIMARY_REQUEST_CLASS,
  INVO_REQUEST_BUDGET_VERSION,
  InvoRequestBudget,
  parseRetryAfterMs,
  type InvoRequestBudgetConfig,
} from '../src/invo-request-budget.js';

function config(overrides: Partial<InvoRequestBudgetConfig> = {}): InvoRequestBudgetConfig {
  return {
    maxRequestsPerSecond: 12,
    burst: 32,
    feedReservedRequestsPerSecond: 4,
    minRequestsPerSecond: 6,
    baseCooldownMs: 2_000,
    maxCooldownMs: 30_000,
    maxRetryAfterCooldownMs: 120_000,
    feedCooldownShare: 0.2,
    rateDecreaseFactor: 0.5,
    rateRecoveryStepPerSecond: 1,
    rateRecoveryIntervalMs: 30_000,
    consecutiveDecayMs: 120_000,
    maxAcquireWaitMs: 10_000,
    ...overrides,
  };
}

/** Sequential virtual clock: enough for single-acquirer pacing assertions. */
function sequentialClock() {
  const clock = { nowMs: 0 };
  return {
    clock,
    now: () => clock.nowMs,
    sleep: async (ms: number) => { clock.nowMs += ms; },
  };
}

test('one bucket paces a single class at exactly the configured burst plus refill', async () => {
  const { clock, now, sleep } = sequentialClock();
  // Pure bucket: no reserve withheld, so this is the pacing contract the removed
  // per-subsystem DirectWatchRequestBudget used to own, now proven on the shared budget.
  const budget = new InvoRequestBudget(config({
    maxRequestsPerSecond: 2, minRequestsPerSecond: 2, burst: 2, feedReservedRequestsPerSecond: 0,
  }), now, sleep);
  const granted: number[] = [];
  for (let index = 0; index < 5; index += 1) {
    await budget.acquire('DIRECT_WATCH');
    granted.push(clock.nowMs);
  }
  assert.deepEqual(granted, [0, 0, 500, 1000, 1500]);
  assert.equal(budget.status(clock.nowMs).classes.DIRECT_WATCH.requests, 5);
});

test('a class in cooldown is rejected without spending a token', async () => {
  const { clock, now, sleep } = sequentialClock();
  const budget = new InvoRequestBudget(config(), now, sleep);
  const decision = budget.note429('DIRECT_WATCH', null, 0);
  assert.equal(decision.cooldownMs, 2_000);
  const before = budget.status(clock.nowMs).availableTokens;
  await assert.rejects(() => budget.acquire('DIRECT_WATCH'), (error: any) => (
    error.status === 429 && error.budgetCooldown === true
      && error.reason === 'class_cooldown' && error.requestClass === 'DIRECT_WATCH'
  ));
  const status = budget.status(clock.nowMs);
  assert.equal(status.availableTokens, before, 'a rejected request must not consume budget');
  assert.equal(status.classes.DIRECT_WATCH.cooldownRejections, 1);
  assert.equal(status.classes.DIRECT_WATCH.requests, 0);
});

test('direct watch can never consume the tokens reserved for the primary feed path', async () => {
  const { clock, now, sleep } = sequentialClock();
  const budget = new InvoRequestBudget(config({
    maxRequestsPerSecond: 8, minRequestsPerSecond: 5, burst: 8, feedReservedRequestsPerSecond: 4,
  }), now, sleep);
  let minTokensAfterDirectGrant = Number.POSITIVE_INFINITY;
  for (let index = 0; index < 12; index += 1) {
    await budget.acquire('DIRECT_WATCH');
    minTokensAfterDirectGrant = Math.min(minTokensAfterDirectGrant, budget.status(clock.nowMs).availableTokens);
  }
  // Every grant required 1 + reserve tokens, so the reserve itself is unreachable.
  assert.ok(minTokensAfterDirectGrant >= 4 - 1e-9,
    `direct watch drew the bucket into the feed reserve (${minTokensAfterDirectGrant})`);
  const feedStartMs = clock.nowMs;
  await budget.acquire('FEED');
  assert.equal(clock.nowMs, feedStartMs, 'the feed must find a token without waiting');
  assert.equal(budget.status(clock.nowMs).classes.FEED.waits, 0);
});

test('the feed may draw the bucket below the reserve; only direct watch is held back', async () => {
  const { clock, now, sleep } = sequentialClock();
  const budget = new InvoRequestBudget(config({
    maxRequestsPerSecond: 4, minRequestsPerSecond: 4, burst: 6, feedReservedRequestsPerSecond: 3,
  }), now, sleep);
  for (let index = 0; index < 6; index += 1) await budget.acquire('FEED');
  assert.equal(clock.nowMs, 0, 'a feed burst up to the bucket size must never be paced');
  assert.ok(budget.status(clock.nowMs).availableTokens < 1);
});

test('direct watch stands aside while a feed request is waiting for capacity', async () => {
  const clock = { nowMs: 0 };
  const waiters: Array<{ dueAtMs: number; resolve: () => void; seq: number }> = [];
  let seq = 0;
  const now = () => clock.nowMs;
  const sleep = (ms: number) => new Promise<void>(resolve => {
    waiters.push({ dueAtMs: clock.nowMs + Math.max(1, ms), resolve, seq: seq += 1 });
  });
  const budget = new InvoRequestBudget(config({
    maxRequestsPerSecond: 4, minRequestsPerSecond: 4, burst: 5, feedReservedRequestsPerSecond: 3,
  }), now, sleep);
  // Drain the bucket with the feed itself, then race a feed and a direct-watch acquirer.
  for (let index = 0; index < 5; index += 1) await budget.acquire('FEED');
  const order: string[] = [];
  const feed = budget.acquire('FEED').then(() => { order.push('FEED'); });
  const direct = budget.acquire('DIRECT_WATCH').then(() => { order.push('DIRECT_WATCH'); });
  for (let tick = 0; tick < 200 && order.length < 2; tick += 1) {
    await new Promise(resolve => setImmediate(resolve));
    if (!waiters.length) continue;
    waiters.sort((a, b) => a.dueAtMs - b.dueAtMs || a.seq - b.seq);
    const next = waiters.shift()!;
    clock.nowMs = Math.max(clock.nowMs, next.dueAtMs);
    next.resolve();
  }
  await Promise.all([feed, direct]);
  assert.deepEqual(order, ['FEED', 'DIRECT_WATCH'], 'reconciliation must not overtake the primary path');
  assert.ok(budget.status(clock.nowMs).classes.DIRECT_WATCH.reserveYields > 0);
});

test('a direct-watch 429 costs the feed only its configured share of the cooldown', () => {
  const { now, sleep } = sequentialClock();
  const budget = new InvoRequestBudget(config(), now, sleep);
  const decision = budget.note429('DIRECT_WATCH', null, 1_000);
  assert.equal(decision.cooldownUntilMs, 3_000);
  assert.equal(decision.peerClass, 'FEED');
  assert.equal(decision.peerCooldownUntilMs, 1_400, 'feed serves 20% of a reconciliation penalty');
  assert.equal(budget.inCooldown('FEED', 1_500), false);
  assert.equal(budget.inCooldown('DIRECT_WATCH', 1_500), true);
  assert.equal(budget.status(1_000).feedPriorityHealthy, true);
});

test('a feed 429 proves the account is limited and gates reconciliation for the full cooldown', () => {
  const { now, sleep } = sequentialClock();
  const budget = new InvoRequestBudget(config(), now, sleep);
  const decision = budget.note429('FEED', null, 0);
  assert.equal(decision.cooldownUntilMs, 2_000);
  assert.equal(decision.peerCooldownUntilMs, 2_000);
  assert.equal(budget.status(0).feedPriorityHealthy, true,
    'equal cooldowns are still feed priority: the feed is never gated longer than direct watch');
});

test('cooldown escalates over consecutive 429s and decays after a quiet window', () => {
  const { now, sleep } = sequentialClock();
  const budget = new InvoRequestBudget(config(), now, sleep);
  assert.equal(budget.note429('DIRECT_WATCH', null, 0).cooldownMs, 2_000);
  assert.equal(budget.note429('DIRECT_WATCH', null, 3_000).cooldownMs, 4_000);
  assert.equal(budget.note429('DIRECT_WATCH', null, 9_000).cooldownMs, 8_000);
  assert.equal(budget.note429('DIRECT_WATCH', null, 20_000).cooldownMs, 16_000);
  assert.equal(budget.note429('DIRECT_WATCH', null, 40_000).cooldownMs, 30_000, 'capped at maxCooldownMs');
  const decayed = budget.note429('DIRECT_WATCH', null, 40_000 + 120_000);
  assert.equal(decayed.consecutive429s, 1);
  assert.equal(decayed.cooldownMs, 2_000, 'a quiet window resets escalation');
});

test('a server Retry-After is honoured in preference to local escalation and bounded', () => {
  const { now, sleep } = sequentialClock();
  const budget = new InvoRequestBudget(config(), now, sleep);
  const honoured = budget.note429('DIRECT_WATCH', 45_000, 0);
  assert.equal(honoured.retryAfterHonored, true);
  assert.equal(honoured.cooldownMs, 45_000, 'Retry-After above the local ceiling is still honoured');
  const clamped = budget.note429('DIRECT_WATCH', 900_000, 200_000);
  assert.equal(clamped.cooldownMs, 120_000, 'a poisoned Retry-After cannot freeze the loop');
  // A Retry-After shorter than the current escalation never shortens the backoff.
  const shorter = budget.note429('DIRECT_WATCH', 100, 250_000);
  assert.equal(shorter.consecutive429s, 3);
  assert.equal(shorter.cooldownMs, 8_000);
  assert.equal(budget.status(250_000).retryAfterHonoredCount, 3);
});

test('the sustained rate decreases multiplicatively on 429 and recovers additively when quiet', async () => {
  const { clock, now, sleep } = sequentialClock();
  const budget = new InvoRequestBudget(config(), now, sleep);
  assert.equal(budget.effectiveRequestsPerSecond, 12);
  assert.equal(budget.note429('DIRECT_WATCH', null, 0).effectiveRequestsPerSecond, 6);
  assert.equal(budget.note429('DIRECT_WATCH', null, 1_000).effectiveRequestsPerSecond, 6,
    'the AIMD floor holds, so a degraded budget never becomes an outage');
  assert.equal(budget.degradedDirectWatchUsableRequestsPerSecond, 2);
  clock.nowMs = 200_000;
  await budget.acquire('FEED');
  const recovered = budget.status(clock.nowMs);
  assert.equal(recovered.effectiveRequestsPerSecond, 7, 'one additive step per quiet interval');
  assert.equal(recovered.rateReductions, 1);
  assert.equal(recovered.rateRecoveries, 1);
});

test('a saturated budget fails closed on a bounded wait instead of hanging a watched loop', async () => {
  const { clock, now, sleep } = sequentialClock();
  const budget = new InvoRequestBudget(config({
    maxRequestsPerSecond: 5, minRequestsPerSecond: 5, burst: 5,
    feedReservedRequestsPerSecond: 4, maxAcquireWaitMs: 100,
  }), now, sleep);
  await budget.acquire('DIRECT_WATCH');
  await assert.rejects(() => budget.acquire('DIRECT_WATCH'), (error: any) => (
    error.status === 429 && error.budgetCooldown === true && error.reason === 'budget_wait_exceeded'
  ));
  assert.ok(clock.nowMs <= 200, 'the wait must be bounded by maxAcquireWaitMs');
  assert.equal(budget.status(clock.nowMs).classes.DIRECT_WATCH.waitExceeded, 1);
  assert.equal(budget.status(clock.nowMs).feedPriorityHealthy, true,
    'only a *feed* wait breach invalidates feed priority');
});

test('unbudgeted token refreshes are charged and reported but never gated', () => {
  const { now, sleep } = sequentialClock();
  const budget = new InvoRequestBudget(config(), now, sleep);
  budget.note429('FEED', null, 0);
  assert.equal(budget.inCooldown('FEED', 0), true);
  budget.chargeUnbudgeted('FEED');
  const status = budget.status(0);
  assert.equal(status.classes.FEED.unbudgetedRequests, 1);
  assert.equal(status.availableTokens, 0, 'the charge cannot drive the bucket negative');
});

test('status reports the coordinated contract and surfaces a feed-priority breach', async () => {
  const { clock, now, sleep } = sequentialClock();
  const healthy = new InvoRequestBudget(config(), now, sleep);
  const status = healthy.status(0);
  assert.equal(status.version, INVO_REQUEST_BUDGET_VERSION);
  assert.equal(status.coordinated, true);
  assert.equal(status.primaryClass, INVO_PRIMARY_REQUEST_CLASS);
  assert.equal(status.reservedForFeedRequestsPerSecond, 4);
  assert.equal(status.directWatchUsableRequestsPerSecond, 8);
  assert.equal(status.feedPriorityHealthy, true);
  assert.deepEqual(status.feedPriorityFailures, []);

  const starved = new InvoRequestBudget(config({
    maxRequestsPerSecond: 2, minRequestsPerSecond: 2, burst: 2,
    feedReservedRequestsPerSecond: 1, maxAcquireWaitMs: 200,
  }), now, sleep);
  await starved.acquire('FEED');
  await starved.acquire('FEED');
  await assert.rejects(() => starved.acquire('FEED'), (error: any) => error.reason === 'budget_wait_exceeded');
  const breached = starved.status(clock.nowMs);
  assert.equal(breached.feedPriorityHealthy, false);
  assert.ok(breached.feedPriorityFailures.includes('feed_budget_wait_exceeded'));
});

test('an unsatisfiable budget configuration is rejected at construction', () => {
  assert.throws(() => new InvoRequestBudget(config({ maxRequestsPerSecond: 0 })), /positive maxRequestsPerSecond/);
  assert.throws(() => new InvoRequestBudget(config({ minRequestsPerSecond: 20 })), /minRequestsPerSecond/);
  assert.throws(() => new InvoRequestBudget(config({ feedReservedRequestsPerSecond: 6 })),
    /below 1 request\/second at the rate floor/);
  assert.throws(() => new InvoRequestBudget(config({ burst: 4, feedReservedRequestsPerSecond: 4 })),
    /burst cannot satisfy the feed reserve/);
  assert.throws(() => new InvoRequestBudget(config({ feedCooldownShare: 0 })), /feed cooldown share/);
  assert.throws(() => new InvoRequestBudget(config({ rateDecreaseFactor: 1 })), /rate decrease factor/);
  assert.throws(() => new InvoRequestBudget(config({ maxRetryAfterCooldownMs: 1_000 })), /retry-after ceiling/);
});

test('Retry-After parsing accepts delta-seconds and HTTP dates and rejects the rest', () => {
  assert.equal(parseRetryAfterMs('3', 0), 3_000);
  assert.equal(parseRetryAfterMs('0.5', 0), 500);
  assert.equal(parseRetryAfterMs(null, 0), null);
  assert.equal(parseRetryAfterMs('', 0), null);
  assert.equal(parseRetryAfterMs('0', 0), null);
  assert.equal(parseRetryAfterMs('-5', 0), null);
  assert.equal(parseRetryAfterMs('not-a-delay', 0), null);
  const nowMs = Date.parse('2026-09-24T00:00:00Z');
  assert.equal(parseRetryAfterMs('Thu, 24 Sep 2026 00:00:10 GMT', nowMs), 10_000);
  assert.equal(parseRetryAfterMs('Thu, 24 Sep 2026 00:00:00 GMT', nowMs), null, 'a past date is not a delay');
});

test('status carries every field the Lane 3 deployment health gate reads', () => {
  // Contract with scripts/validate_lane3_shadow_health.py: the gate fails closed on a
  // missing or uncoordinated budget block, so these key names are part of the interface.
  const status = new InvoRequestBudget(config()).status(Date.now());
  assert.equal(status.coordinated, true);
  assert.equal(status.primaryClass, 'FEED');
  assert.equal(typeof status.feedPriorityHealthy, 'boolean');
  assert.ok(Array.isArray(status.feedPriorityFailures));
  assert.equal(typeof status.reservedForFeedRequestsPerSecond, 'number');
  assert.equal(typeof status.cooldownRemainingMs.FEED, 'number');
  assert.equal(typeof status.cooldownRemainingMs.DIRECT_WATCH, 'number');
  assert.equal(status.classes.FEED.waitExceeded, 0);
  assert.equal(status.classes.DIRECT_WATCH.waitExceeded, 0);
  assert.ok(status.reservedForFeedRequestsPerSecond >= 1,
    'the gate rejects a deployment whose feed reserve is not configured');
});
