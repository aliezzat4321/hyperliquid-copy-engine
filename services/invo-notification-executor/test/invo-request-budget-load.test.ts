import assert from 'node:assert/strict';
import test from 'node:test';
import { InvoRequestBudget, type InvoRequestBudgetConfig } from '../src/invo-request-budget.js';
import { directWatchCapacity, runConcurrentHydrations } from '../src/elite-direct-watch.js';
import { directWatchdogLimitMs } from '../src/loop-watchdog.js';

/**
 * Load regression for the production resident population Lane 3 is heading for. The
 * qualified-elite population is uncapped by design (there is no trader cap), so the test
 * runs the deployed defaults against 41 residents — more than the transport can sweep
 * inside its OPEN deadline — and proves that the *feed* is unaffected anyway. Direct-watch
 * oversubscription must degrade direct-watch's own freshness and nothing else.
 */
const RESIDENTS = 41;

/** Deployed defaults (see loadConfig / .env.example). */
const PRODUCTION_BUDGET: InvoRequestBudgetConfig = {
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
};
const DIRECT_WATCH_CONCURRENCY = 16;
const OPEN_PAGES = 3;
const CLOSED_PAGES = 2;
const REQUESTS_PER_RESIDENT = OPEN_PAGES + CLOSED_PAGES;

/**
 * Deterministic virtual clock. Only the injected `sleep` advances time, so a full sweep of
 * 41 residents at 16-way concurrency is replayed exactly rather than raced in wall time.
 */
function virtualClock() {
  const state = { nowMs: 0 };
  const waiters: Array<{ dueAtMs: number; resolve: () => void; seq: number }> = [];
  let seq = 0;
  return {
    state,
    now: () => state.nowMs,
    sleep: (ms: number) => new Promise<void>(resolve => {
      waiters.push({ dueAtMs: state.nowMs + Math.max(1, ms), resolve, seq: seq += 1 });
    }),
    async run(maxTicks = 500_000) {
      for (let tick = 0; tick < maxTicks; tick += 1) {
        await new Promise(resolve => setImmediate(resolve));
        if (!waiters.length) return;
        waiters.sort((a, b) => a.dueAtMs - b.dueAtMs || a.seq - b.seq);
        const next = waiters.shift()!;
        state.nowMs = Math.max(state.nowMs, next.dueAtMs);
        next.resolve();
      }
      throw new Error('virtual clock did not settle');
    },
  };
}

interface SweepResult {
  budget: InvoRequestBudget;
  feedWaitsMs: number[];
  feedGranted: number;
  feedRejected: unknown[];
  directGranted: number;
  directRejected: unknown[];
  minTokensAfterDirectGrant: number;
  finishedAtMs: number;
}

/**
 * Runs one direct-watch sweep of `residents` targets concurrently with `feedPolls` primary
 * feed page reads spaced `feedSpacingMs` apart, against a single shared budget.
 */
async function sweep(options: {
  residents?: number;
  feedPolls?: number;
  feedSpacingMs?: number;
  feedPagesPerPoll?: number;
  budget?: Partial<InvoRequestBudgetConfig>;
  onDirectRequest?: (budget: InvoRequestBudget, atMs: number) => void;
} = {}): Promise<SweepResult> {
  const residents = options.residents ?? RESIDENTS;
  const feedPolls = options.feedPolls ?? 12;
  const feedSpacingMs = options.feedSpacingMs ?? 1_000;
  const feedPagesPerPoll = options.feedPagesPerPoll ?? 1;
  const clock = virtualClock();
  const budget = new InvoRequestBudget(
    { ...PRODUCTION_BUDGET, ...options.budget }, clock.now, clock.sleep,
  );
  const result: SweepResult = {
    budget, feedWaitsMs: [], feedGranted: 0, feedRejected: [],
    directGranted: 0, directRejected: [],
    minTokensAfterDirectGrant: Number.POSITIVE_INFINITY, finishedAtMs: 0,
  };

  const targets = Array.from({ length: residents }, (_, index) => ({
    target: { portfolioId: `p${String(index).padStart(3, '0')}` },
  }));
  const directSweep = runConcurrentHydrations(
    targets, DIRECT_WATCH_CONCURRENCY, () => {},
    async () => {
      for (let request = 0; request < REQUESTS_PER_RESIDENT; request += 1) {
        try {
          await budget.acquire('DIRECT_WATCH');
        } catch (error) {
          result.directRejected.push(error);
          throw error;
        }
        result.directGranted += 1;
        result.minTokensAfterDirectGrant = Math.min(
          result.minTokensAfterDirectGrant, budget.status(clock.state.nowMs).availableTokens,
        );
        options.onDirectRequest?.(budget, clock.state.nowMs);
      }
    },
  );

  const feedLoop = (async () => {
    for (let poll = 0; poll < feedPolls; poll += 1) {
      await clock.sleep(feedSpacingMs);
      for (let page = 0; page < feedPagesPerPoll; page += 1) {
        const startedAtMs = clock.state.nowMs;
        try {
          await budget.acquire('FEED');
          result.feedGranted += 1;
          result.feedWaitsMs.push(clock.state.nowMs - startedAtMs);
        } catch (error) {
          result.feedRejected.push(error);
        }
      }
    }
  })();

  const settled = Promise.allSettled([directSweep, feedLoop]);
  await clock.run();
  await settled;
  result.finishedAtMs = clock.state.nowMs;
  return result;
}

test('41 residents saturating direct watch never delay a primary feed request', async () => {
  const run = await sweep();
  assert.equal(run.directGranted, RESIDENTS * REQUESTS_PER_RESIDENT);
  assert.equal(run.feedGranted, 12, 'every primary feed page must be admitted');
  assert.deepEqual(run.feedRejected, []);
  // The structural guarantee: direct watch can only take a token while more than the
  // reserved feed tokens remain, so a feed request always finds one immediately.
  assert.deepEqual(run.feedWaitsMs, new Array(12).fill(0));
  assert.ok(run.minTokensAfterDirectGrant >= PRODUCTION_BUDGET.feedReservedRequestsPerSecond - 1e-9,
    `direct watch drew into the feed reserve (${run.minTokensAfterDirectGrant})`);
  const status = run.budget.status(run.finishedAtMs);
  assert.equal(status.classes.FEED.waits, 0);
  assert.equal(status.classes.FEED.waitExceeded, 0);
  assert.equal(status.feedPriorityHealthy, true);
  assert.equal(status.feedPriorityEverBreached, false);
  assert.equal(status.total429s, 0, 'coordinated pacing must not need a 429 to stay inside the ceiling');
});

test('a 20-page feed backfill outruns 41 residents of reconciliation traffic', async () => {
  // Worst realistic primary burst: a full NOTIFICATION_TRADER_FEED_MAX_PAGES backfill
  // arriving while a 41-resident sweep is already in flight.
  const run = await sweep({ feedPolls: 1, feedSpacingMs: 250, feedPagesPerPoll: 20 });
  assert.equal(run.feedGranted, 20);
  assert.deepEqual(run.feedRejected, []);
  const status = run.budget.status(run.finishedAtMs);
  const feedWallMs = Math.max(...run.feedWaitsMs);
  // 20 pages need at most the burst plus refill of the shortfall; direct watch may not
  // extend that, because it can never hold the reserve.
  assert.ok(feedWallMs <= 2_000, `feed backfill was throttled for ${feedWallMs}ms`);
  assert.ok(status.classes.DIRECT_WATCH.reserveYields > 0,
    'reconciliation must be observed standing aside for the backfill');
  assert.equal(status.feedPriorityHealthy, true);
  assert.equal(status.feedPriorityEverBreached, false);
  assert.equal(run.directGranted, RESIDENTS * REQUESTS_PER_RESIDENT,
    'yielding must not drop reconciliation work, only defer it');
});

test('a mid-sweep 429 stops 41 residents of reconciliation while the feed resumes on its share', async () => {
  let injected = false;
  const run = await sweep({
    feedPolls: 12,
    onDirectRequest: (budget, atMs) => {
      if (injected) return;
      injected = true;
      // Adaptive cooldown as the service applies it, with a server-stated Retry-After.
      budget.note429('DIRECT_WATCH', 5_000, atMs);
    },
  });
  assert.ok(run.directRejected.length > 0, 'reconciliation must fail closed inside the cooldown');
  assert.ok(run.directGranted < RESIDENTS * REQUESTS_PER_RESIDENT);
  for (const error of run.directRejected) {
    assert.equal((error as any).status, 429);
    assert.equal((error as any).budgetCooldown, true);
  }
  const status = run.budget.status(run.finishedAtMs);
  assert.equal(status.cooldownRemainingMs.FEED, 0, 'the feed must be out of cooldown long before direct watch');
  assert.equal(status.retryAfterObservedCount, 1);
  assert.equal(status.retryAfterHonoredCount, 1,
    'the 5s server delay exceeded the 2s local escalation, so it is what actually gated');
  assert.equal(status.effectiveRequestsPerSecond, 6, 'multiplicative decrease down to the AIMD floor');
  assert.equal(status.feedPriorityHealthy, true);
  assert.equal(status.feedPriorityEverBreached, false,
    'a reconciliation 429 must not starve a single feed request');
  // The feed keeps polling through the reconciliation penalty: it serves only 1s of the 5s
  // cooldown, so at 1s spacing at most one poll is lost.
  assert.ok(run.feedGranted >= 11, `feed only completed ${run.feedGranted}/12 polls`);
});

test('41 residents are reported as oversubscribed rather than silently starving the feed', () => {
  const capacityInput = {
    scanMs: 3_000,
    maxOpenHydratesPerScan: 24,
    openPollMs: 18_000,
    maxClosedHydratesPerScan: 24,
    closedPollMs: 60_000,
    requestTimeoutMs: 2_000,
    maxAttemptsPerPage: 2,
    openMaxPages: OPEN_PAGES,
    closedMaxPages: CLOSED_PAGES,
    fixedOverheadMs: 2_000,
    concurrency: DIRECT_WATCH_CONCURRENCY,
    requestBudgetPerSecond: PRODUCTION_BUDGET.maxRequestsPerSecond,
    requestBudgetBurst: PRODUCTION_BUDGET.burst,
    fixedReserveRequestsPerSecond: PRODUCTION_BUDGET.feedReservedRequestsPerSecond,
  };
  const nominal = directWatchCapacity(capacityInput);
  const degraded = directWatchCapacity({
    ...capacityInput, requestBudgetPerSecond: PRODUCTION_BUDGET.minRequestsPerSecond,
  });
  assert.ok(nominal.provenResidentCap >= 1);
  assert.ok(nominal.provenResidentCap < RESIDENTS,
    'the honest reading of the deployed transport is that 41 residents are oversubscribed');
  assert.ok(degraded.provenResidentCap >= 1,
    'even a fully degraded coordinated budget must still prove one resident, not an outage');
  assert.ok(degraded.provenResidentCap <= nominal.provenResidentCap);
  // The feed reserve is charged against direct-watch capacity, never the reverse.
  const unreserved = directWatchCapacity({ ...capacityInput, fixedReserveRequestsPerSecond: 0 });
  assert.ok(unreserved.provenResidentCap >= nominal.provenResidentCap);
});

test('the direct-watch watchdog bound holds at the degraded rate and ignores resident count', () => {
  const watchdogInput = {
    openHydrates: 24, closedHydrates: 24,
    openMaxPages: OPEN_PAGES, closedMaxPages: CLOSED_PAGES, maxAttemptsPerPage: 2,
    concurrency: DIRECT_WATCH_CONCURRENCY, requestTimeoutMs: 2_000,
    requestBudgetBurst: PRODUCTION_BUDGET.burst,
    fixedReserveRequestsPerSecond: PRODUCTION_BUDGET.feedReservedRequestsPerSecond,
    fixedOverheadMs: 2_000, postScanFlushBudgetMs: 25_000, processingMarginMs: 15_000,
  };
  const atCeiling = directWatchdogLimitMs({
    ...watchdogInput, requestBudgetPerSecond: PRODUCTION_BUDGET.maxRequestsPerSecond,
  });
  const atFloor = directWatchdogLimitMs({
    ...watchdogInput, requestBudgetPerSecond: PRODUCTION_BUDGET.minRequestsPerSecond,
  });
  assert.ok(Number.isFinite(atFloor) && atFloor > atCeiling,
    'an adaptive rate reduction lengthens a legitimate scan, so the bound must be taken at the floor');
  // Per-scan work is bounded by the hydration caps, not by how many residents exist, so a
  // 41+ resident population cannot inflate the bound or trip a false stall.
  const at41Residents = directWatchdogLimitMs({
    ...watchdogInput, requestBudgetPerSecond: PRODUCTION_BUDGET.minRequestsPerSecond,
  });
  assert.equal(at41Residents, atFloor);
});
