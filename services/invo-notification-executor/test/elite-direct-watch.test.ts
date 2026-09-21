import assert from 'node:assert/strict';
import { appendFileSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import {
  EliteDirectWatchState,
  establishClosedBaseline,
  fetchCompleteOpenInvestments,
  closedBoundaryProof,
  closedSignalsAfterBoundary,
  classifyClosedHydrationRows,
  directInvestmentRows,
  loadEliteDirectTargets,
  isMissedPreDemotionOpen,
  planClosedHydrations,
  planDeadlineHydrations,
  planDirectHydrations,
  DirectWatchRequestBudget,
  runConcurrentHydrations,
  runIsolatedHydrations,
  retiringOpenDispositions,
  signalsFromDirectInvestments,
  unownedCloseEvidence,
  validateClosedPageOrdering,
  validateDirectWatchCapacity,
  type EliteDirectTarget,
} from '../src/elite-direct-watch.js';
import { ELITE_SELECTOR_VERSION } from '../src/portfolio-candidates.js';

const BASE = 1_780_000_000_000;

const target: EliteDirectTarget = {
  portfolioId: 'p1', ownerId: 'o1', username: 'elite', sourceFilter: 'trending', score: 100,
};

function openRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 'inv1', baseId: 'base1', baseShortId: 'short1', ticker: 'SUI',
    verifiedTrade: true, isOpen: true, directionLong: true, leverage: 7,
    entryPrice: 0.75, entrySize: 2, createdAt: BASE + 100, updatedAt: BASE + 100,
    portfolio: { id: 'p1' }, ...overrides,
  };
}

function retirementLifecycle(overrides: Partial<{
  hasObservedOpen: (sourceBaseId: string) => boolean;
  isManagedSource: (sourceBaseId: string) => boolean;
  hasHandledClose: (sourceBaseId: string) => boolean;
  hasSeen: (key: string) => boolean;
}> = {}) {
  return {
    hasObservedOpen: () => false,
    isManagedSource: () => false,
    hasHandledClose: () => false,
    hasSeen: () => false,
    ...overrides,
  };
}

function retireTarget(
  state: EliteDirectWatchState, atMs: number, retirementGraceMs: number,
  owned = new Set<string>(),
) {
  state.syncTargets([], owned, atMs - 1, true, retirementGraceMs, new Set(['p1']),
    Number.POSITIVE_INFINITY, 2, 0, atMs - 1);
  state.syncTargets([], owned, atMs, true, retirementGraceMs, new Set(['p1']),
    Number.POSITIVE_INFINITY, 2, 0, atMs);
}
test('startup baseline never replays pre-baseline investments', () => {
  const signals = signalsFromDirectInvestments(
    [openRow({ createdAt: BASE - 100, updatedAt: BASE - 100 })], [], target, BASE, BASE + 10,
  );
  assert.deepEqual(signals, []);
});

test('fresh verified open becomes a canonical direct elite signal', () => {
  const [signal] = signalsFromDirectInvestments([openRow()], [], target, BASE, BASE + 120);
  assert.ok(signal);
  assert.equal(signal.action, 'open');
  assert.equal(signal.portfolioId, 'p1');
  assert.equal(signal.sourceBaseId, 'base1');
  assert.equal(signal.sourceTimeMs, BASE + 100);
  assert.equal(signal.entrySize, 2);
});

test('fresh increase uses only the proven positive source-size delta', () => {
  const row = openRow({
    createdAt: BASE - 100,
    updatedAt: BASE + 200,
    entrySize: 3.5,
    changes: { simIncrease: true, entrySize: 2 },
  });
  const [signal] = signalsFromDirectInvestments([row], [], target, BASE, BASE + 210);
  assert.ok(signal);
  assert.equal(signal.action, 'increase');
  assert.equal(signal.entrySize, 1.5);
});
test('fresh owned close is emitted but closed history before the watermark is not', () => {
  const closed = openRow({
    isOpen: false,
    closingPrice: 0.8,
    createdAt: BASE - 100,
    updatedAt: BASE + 300,
    closedAt: BASE + 300,
  });
  const [signal] = signalsFromDirectInvestments([], [closed], target, BASE, BASE + 310);
  assert.ok(signal);
  assert.equal(signal.action, 'close');
  assert.equal(signal.closingPrice, 0.8);
  assert.deepEqual(signalsFromDirectInvestments([], [closed], target, BASE + 400, BASE + 410), []);
});

test('selector observation establishes baseline first and hydrates only on later change', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-watch-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  assert.equal(state.observeSelector('p1', BASE + 10).hydrate, false);
  assert.equal(state.observeSelector('p1', BASE + 10).hydrate, false);
  assert.equal(state.observeSelector('p1', BASE + 20).hydrate, true);
  state.commitHydration('p1', BASE + 20, BASE + 20);
  assert.equal(state.observeSelector('p1', BASE + 20).hydrate, false);
});

test('candidate loader tracks only fresh elite portfolios', () => {
  const dir = mkdtempSync(join(tmpdir(), 'elite-candidates-'));
  const path = join(dir, 'candidates.json');
  writeFileSync(path, JSON.stringify({
    selectorVersion: ELITE_SELECTOR_VERSION,
    lastObservedAtMs: BASE,
    portfolios: {
      p1: { ...target, selectorVersion: ELITE_SELECTOR_VERSION, observedAtMs: BASE, bucket: 'ELITE_CANDIDATE' },
      p2: { portfolioId: 'p2', selectorVersion: ELITE_SELECTOR_VERSION, ownerId: 'o2', username: 'wide', sourceFilter: 'all', observedAtMs: BASE, bucket: 'RESEARCH_WIDE' },
    },
  }));
  const fresh = loadEliteDirectTargets(path, BASE + 10, 20_000);
  assert.equal(fresh.stale, false);
  assert.deepEqual(fresh.targets, [target]);
  const stale = loadEliteDirectTargets(path, BASE + 20_001, 20_000);
  assert.equal(stale.stale, true);
  assert.deepEqual(stale.targets, []);
});

test('bounded discovery absence is not demotion; only same-cycle non-elite is explicit', () => {
  const dir = mkdtempSync(join(tmpdir(), 'elite-authoritative-universe-'));
  const path = join(dir, 'candidates.json');
  writeFileSync(path, JSON.stringify({
    selectorVersion: ELITE_SELECTOR_VERSION,
    lastObservedAtMs: BASE + 10,
    portfolios: {
      p1: { ...target, selectorVersion: ELITE_SELECTOR_VERSION, observedAtMs: BASE, bucket: 'ELITE_CANDIDATE' },
      p2: { ...target, selectorVersion: ELITE_SELECTOR_VERSION, portfolioId: 'p2', observedAtMs: BASE + 10, bucket: 'RESEARCH_WIDE' },
    },
  }));
  const fresh = loadEliteDirectTargets(path, BASE + 11, 20_000);
  assert.equal(fresh.stale, false);
  assert.deepEqual(fresh.targets, []);
  assert.deepEqual(fresh.demotedPortfolioIds, ['p2']);
});

test('invalid fresh candidate state is non-authoritative and preserves existing targets', () => {
  const dir = mkdtempSync(join(tmpdir(), 'elite-invalid-candidates-'));
  const candidatePath = join(dir, 'candidates.json');
  const watch = new EliteDirectWatchState(join(dir, 'watch.json'));
  watch.syncTargets([target], new Set(), BASE);

  const invalidStates = [
    { selectorVersion: 'old-selector', lastObservedAtMs: BASE + 10, portfolios: {} },
    { selectorVersion: ELITE_SELECTOR_VERSION, lastObservedAtMs: BASE + 20, portfolios: {} },
    { selectorVersion: ELITE_SELECTOR_VERSION, lastObservedAtMs: BASE + 10, portfolios: [] },
    { selectorVersion: ELITE_SELECTOR_VERSION, lastObservedAtMs: BASE + 10, portfolios: {
      p1: { ...target, portfolioId: 'different', selectorVersion: ELITE_SELECTOR_VERSION,
        observedAtMs: BASE + 10, bucket: 'RESEARCH_WIDE' },
    } },
    { selectorVersion: ELITE_SELECTOR_VERSION, lastObservedAtMs: BASE + 10, portfolios: {
      p1: { ...target, selectorVersion: ELITE_SELECTOR_VERSION, observedAtMs: BASE + 10,
        bucket: 'ELITE_CANDIDATE', ownerId: '' },
    } },
  ];

  for (const [index, state] of invalidStates.entries()) {
    writeFileSync(candidatePath, JSON.stringify(state));
    const loaded = loadEliteDirectTargets(candidatePath, BASE + 11, 20_000);
    assert.equal(loaded.stale, true, `case ${index} must be non-authoritative`);
    assert.ok(loaded.validationError, `case ${index} exposes fail-closed telemetry`);
    assert.deepEqual(loaded.targets, []);
    assert.deepEqual(loaded.demotedPortfolioIds, []);
    watch.syncTargets(loaded.targets, new Set(), BASE + 11, !loaded.stale,
      120_000, new Set(loaded.demotedPortfolioIds));
    assert.equal(watch.targets()[0]?.portfolioId, 'p1');
    assert.equal(watch.targets()[0]?.lifecycle, 'ENROLLING');
  }
});

test('owned portfolio remains directly watched after demotion and restart', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-owned-watch-')), 'state.json');
  const first = new EliteDirectWatchState(path);
  first.syncTargets([target], new Set(), BASE);
  first.observeSelector('p1', BASE + 10);

  const restarted = new EliteDirectWatchState(path);
  restarted.syncTargets([], new Set(['p1']), BASE + 100);
  assert.equal(restarted.targets().length, 1);
  assert.equal(restarted.targets()[0].portfolioId, 'p1');

  restarted.syncTargets([], new Set(), BASE + 200);
  assert.equal(restarted.targets().length, 1, 'retirement requires close-drain proof and grace');
});

test('overdue periodic targets outrank repeated selector-change hints', () => {
  const makeTarget = (portfolioId: string, lastFallbackPollAtMs: number) => ({
    ...target,
    portfolioId,
    baselineAtMs: BASE,
    processedThroughMs: BASE,
    selectorInitialized: true,
    lastSelectorUpdatedAtMs: BASE,
    lastFallbackPollAtMs,
    closedHistoryInitialized: true,
    closedProcessedThroughMs: BASE,
    closedBoundaryIds: [],
    lastClosedPollAtMs: BASE,
  });
  const overdueOldest = makeTarget('periodic-oldest', BASE - 60_000);
  const overdueNewer = makeTarget('periodic-newer', BASE - 50_000);
  const selectorOnly = makeTarget('selector-only', BASE - 1_000);
  const selectorChanges = new Map([
    [selectorOnly.portfolioId, BASE + 10],
    [overdueNewer.portfolioId, BASE + 20],
  ]);

  const first = planDirectHydrations(
    [selectorOnly, overdueNewer, overdueOldest], selectorChanges, BASE, 25_000, 1,
  );
  assert.equal(first[0].target.portfolioId, 'periodic-oldest');
  assert.equal(first[0].reason, 'periodic_direct_poll');

  overdueOldest.lastFallbackPollAtMs = BASE;
  const second = planDirectHydrations(
    [selectorOnly, overdueNewer, overdueOldest], selectorChanges, BASE, 25_000, 1,
  );
  assert.equal(second[0].target.portfolioId, 'periodic-newer');
  assert.equal(second[0].reason, 'periodic_direct_poll');
  assert.equal(second[0].selectorUpdatedAtMs, BASE + 20);
});

test('recorded direct-poll attempts rotate bounded periodic service across targets', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-watch-fairness-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  const secondTarget = { ...target, portfolioId: 'p2' };
  state.syncTargets([target, secondTarget], new Set(), BASE - 60_000);
  state.commitClosedHydration('p1', [], BASE - 60_000);
  state.commitClosedHydration('p2', [], BASE - 60_000);

  const first = planDirectHydrations(state.targets(), new Map(), BASE, 25_000, 1);
  assert.equal(first[0].target.portfolioId, 'p1');
  state.noteFallbackPoll('p1', BASE);

  const second = planDirectHydrations(state.targets(), new Map(), BASE, 25_000, 1);
  assert.equal(second[0].target.portfolioId, 'p2');
});

test('OPEN hydration is blocked until the CLOSED cursor is initialized', () => {
  const stored = {
    ...target, lifecycle: 'ACTIVE' as const, retiredAtMs: null, retireAfterMs: null,
    baselineAtMs: BASE, processedThroughMs: BASE, selectorInitialized: true,
    lastSelectorUpdatedAtMs: BASE, lastFallbackPollAtMs: 0,
    closedHistoryInitialized: false, closedProcessedThroughMs: 0,
    closedBoundaryIds: [], lastClosedPollAtMs: 0,
  };
  assert.equal(planDirectHydrations([stored], new Map(), BASE + 30_000, 20_000, 1).length, 0);
  stored.closedHistoryInitialized = true;
  stored.closedProcessedThroughMs = BASE;
  assert.equal(planDirectHydrations([stored], new Map(), BASE + 30_000, 20_000, 1).length, 1);
});

test('fresh CLOSED row missing closingPrice blocks watermark classification until it becomes emittable', () => {
  const closed = openRow({
    isOpen: false, baseId: 'close-x', id: 'close-x', createdAt: BASE + 10,
    closedAt: BASE + 100, updatedAt: BASE + 100, closingPrice: null,
  });
  const blocked = classifyClosedHydrationRows([closed], target, BASE, [], BASE + 110);
  assert.equal(blocked.signals.length, 0);
  assert.equal(blocked.freshRowCount, 1);
  assert.equal(blocked.unemittableFreshRows[0]?.reason, 'closing_price_unavailable');

  const ready = classifyClosedHydrationRows(
    [{ ...closed, closingPrice: 0.8 }], target, BASE, [], BASE + 120,
  );
  assert.equal(ready.signals.length, 1);
  assert.equal(ready.unemittableFreshRows.length, 0);
});

test('same resulting source size cannot be copied twice if only updatedAt changes later', () => {
  const first = openRow({
    createdAt: BASE - 100,
    updatedAt: BASE + 200,
    entrySize: 3.5,
    changes: { simIncrease: true, entrySize: 2 },
  });
  const laterMetadataUpdate = { ...first, updatedAt: BASE + 500 };
  const [a] = signalsFromDirectInvestments([first], [], target, BASE, BASE + 210);
  const [b] = signalsFromDirectInvestments([laterMetadataUpdate], [], target, BASE, BASE + 510);
  assert.ok(a && b);
  assert.equal(a.key, b.key);
  assert.equal(a.entrySize, 1.5);
  assert.equal(b.entrySize, 1.5);
});

test('sanitized captured investment shapes preserve open/increase and owned-close semantics', () => {
  const fixture = JSON.parse(readFileSync(
    new URL('../../test/fixtures/invo-read-only-captured-shapes.json', import.meta.url),
    'utf8',
  ));
  const capturedTarget = {
    portfolioId: 'portfolio-direct', ownerId: 'owner-direct', username: 'captured-shape', sourceFilter: 'fire_moves', score: 100,
  };
  const signals = signalsFromDirectInvestments(
    fixture.investments.open.investmentsTicker,
    fixture.investments.closed.investmentsTicker,
    capturedTarget,
    1789685682200,
    1789685684000,
  );
  assert.deepEqual(signals.map(row => row.action), ['increase', 'close']);
  assert.equal(signals[0].entrySize, 1);
  assert.equal(signals[1].closingPrice, 1.1);
});

test('closed-only round trip after a baseline is evidence, never a replayed shadow open', () => {
  const closed = openRow({
    isOpen: false, closingPrice: 0.8, createdAt: BASE + 100, updatedAt: BASE + 200, closedAt: BASE + 200,
  });
  const signals = closedSignalsAfterBoundary([closed], target, BASE, [], BASE + 300);
  assert.equal(signals.length, 1);
  assert.equal(signals[0].action, 'close');
  assert.equal(signals.some(signal => signal.action === 'open'), false);
  assert.deepEqual(unownedCloseEvidence(signals[0], false), {
    type: 'missed_short_roundtrip',
    reason: 'open_and_close_not_observed_while_open',
    lifecycleCopyability: 'NON_COPYABLE_CLOSED_ONLY',
    reconstructedOpenExecuted: false,
    sourceCreatedAtMs: BASE + 100,
    sourceClosedAtMs: BASE + 200,
    portfolioId: 'p1',
  });
});

test('pre-enrollment close is ignored evidence and not a selected-elite recall miss', () => {
  const closed = openRow({ isOpen: false, closingPrice: 0.8,
    createdAt: BASE - 100, updatedAt: BASE + 200, closedAt: BASE + 200 });
  const [signal] = closedSignalsAfterBoundary([closed], target, BASE, [], BASE + 300);
  assert.deepEqual(unownedCloseEvidence(signal, false, BASE), {
    type: 'pre_enrollment_close_ignored',
    reason: 'source_open_predates_direct_watch_admission',
    lifecycleCopyability: 'NON_COPYABLE_PRE_ENROLLMENT',
    reconstructedOpenExecuted: false,
    sourceCreatedAtMs: BASE - 100,
    sourceClosedAtMs: BASE + 200,
    portfolioId: 'p1',
  });
});

test('direct investment response schema rejects malformed HTTP-200 success envelopes', () => {
  assert.deepEqual(directInvestmentRows({ investmentsTicker: [] }), []);
  for (const malformed of [null, {}, [], { investmentsTicker: null }, { investmentsTicker: {} }]) {
    assert.throws(() => directInvestmentRows(malformed), /investmentsTicker must be an array/);
  }
});

test('startup cap and admission health publish zero persisted ACTIVE admissions until recovery', () => {
  const dir = mkdtempSync(join(tmpdir(), 'elite-startup-cap-'));
  const path = join(dir, 'state.json');
  const admissionPath = join(dir, 'admissions.json');
  const first = new EliteDirectWatchState(path, admissionPath, 3);
  const targets = Array.from({ length: 3 }, (_, index) => ({ ...target, portfolioId: `p${index}` }));
  first.syncTargets(targets, new Set(), BASE, true, 120_000, new Set(), 3, 2, 600_000, BASE,
    ELITE_SELECTOR_VERSION, true);
  for (const row of targets) {
    first.commitClosedHydration(row.portfolioId, [], BASE + 1);
    first.commitOpenBaseline(row.portfolioId, [], BASE + 2);
  }
  first.setAdmissionHealth(true);
  assert.equal(Object.keys(JSON.parse(readFileSync(admissionPath, 'utf8')).rows).length, 3);

  const restarted = new EliteDirectWatchState(path, admissionPath, 2);
  assert.equal(restarted.status().activeTargetCount, 3);
  assert.equal(restarted.status().admissionsHealthy, false);
  assert.deepEqual(JSON.parse(readFileSync(admissionPath, 'utf8')).rows, {});
  restarted.syncTargets([], new Set(), BASE + 3, false, 120_000, new Set(), 999,
    2, 600_000, BASE + 3, ELITE_SELECTOR_VERSION, true);
  assert.deepEqual(JSON.parse(readFileSync(admissionPath, 'utf8')).rows, {},
    'missing/stale/malformed candidate authority cannot make startup permissive');
});

test('clean startup enrolls up to proven cap while publication is suspended, then publishes only healthy ACTIVE rows', () => {
  const dir = mkdtempSync(join(tmpdir(), 'elite-clean-enrollment-'));
  const admissionPath = join(dir, 'admissions.json');
  const watch = new EliteDirectWatchState(join(dir, 'state.json'), admissionPath, 16);
  const candidates = Array.from({ length: 20 }, (_, index) => ({
    ...target, portfolioId: `p${String(index).padStart(2, '0')}`, score: 100 - index,
  }));

  watch.setAdmissionHealth(false, 'scan_in_progress');
  watch.syncTargets(candidates, new Set(), BASE, true, 120_000, new Set(), 16, 2, 600_000,
    BASE, ELITE_SELECTOR_VERSION, true);
  assert.equal(watch.status().residentCount, 16);
  assert.equal(watch.status().enrollingTargetCount, 16);
  assert.equal(watch.status().deferredCount, 4);
  assert.equal(watch.status().admissionPublished, false);
  assert.deepEqual(JSON.parse(readFileSync(admissionPath, 'utf8')).rows, {});

  for (const row of watch.targets()) {
    watch.commitClosedHydration(row.portfolioId, [], BASE + 1);
    watch.commitOpenBaseline(row.portfolioId, [], BASE + 2);
  }
  assert.equal(watch.status().activeTargetCount, 16);
  assert.equal(watch.status().admissionPublished, false,
    'baseline completion during a scan remains non-authorizing');
  watch.setAdmissionHealth(true);
  assert.equal(Object.keys(JSON.parse(readFileSync(admissionPath, 'utf8')).rows).length, 16);
  assert.equal(watch.status().scanHealthy, true);
  assert.equal(watch.status().admissionPublished, true);

  watch.setAdmissionHealth(false, 'rate_limit_cooldown');
  assert.deepEqual(JSON.parse(readFileSync(admissionPath, 'utf8')).rows, {});
  assert.equal(watch.status().residentCount, 16, 'unhealthy scans revoke authorization without deleting baselines');
  assert.equal(watch.status().closedInitializedCount, 16);
  assert.equal(watch.status().activeTargetCount, 16);
  assert.equal(watch.status().admissionPublished, false);
});

test('attempt rotation cannot refresh admission health; suspension and successful recovery are atomic', () => {
  const dir = mkdtempSync(join(tmpdir(), 'elite-success-freshness-'));
  const admissionPath = join(dir, 'admissions.json');
  const state = new EliteDirectWatchState(join(dir, 'state.json'), admissionPath, 1);
  state.syncTargets([target], new Set(), BASE, true, 120_000, new Set(), 1, 2, 600_000, BASE,
    ELITE_SELECTOR_VERSION, true);
  state.commitClosedHydration('p1', [], BASE + 1);
  state.commitOpenBaseline('p1', [], BASE + 2);
  const successfulAt = state.status().oldestOpenPollAtMs;
  state.noteFallbackPoll('p1', BASE + 50_000);
  state.noteClosedPoll('p1', BASE + 50_000);
  assert.equal(state.status().oldestOpenPollAtMs, successfulAt,
    'failed attempts only rotate scheduling and never refresh health');
  state.setAdmissionHealth(false, 'rate_limit_cooldown');
  assert.deepEqual(JSON.parse(readFileSync(admissionPath, 'utf8')).rows, {});
  state.commitHydration('p1', BASE + 50_000);
  state.commitClosedHydration('p1', [], BASE + 50_000);
  state.setAdmissionHealth(true);
  assert.deepEqual(Object.keys(JSON.parse(readFileSync(admissionPath, 'utf8')).rows), ['p1']);
});

test('snapshot sequence ignores stale journal row from rename-before-truncate crash window', () => {
  const dir = mkdtempSync(join(tmpdir(), 'elite-journal-seq-'));
  const path = join(dir, 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  state.commitHydration('p1', BASE + 10);
  state.syncTargets([{ ...target, username: 'new-name' }], new Set(), BASE + 20);
  const snapshot = JSON.parse(readFileSync(path, 'utf8'));
  const staleTarget = { ...snapshot.targets.p1, username: 'stale-name', processedThroughMs: BASE + 5 };
  appendFileSync(`${path}.journal.jsonl`, `${JSON.stringify({ version: 2,
    seq: snapshot.snapshotAppliedJournalSeq, portfolioId: 'p1', target: staleTarget })}\n`);
  const restarted = new EliteDirectWatchState(path);
  assert.equal(restarted.targets()[0].username, 'new-name');
  assert.equal(restarted.targets()[0].processedThroughMs, BASE + 10);
  restarted.commitHydration('p1', BASE + 30);
  const newestJournal = JSON.parse(readFileSync(`${path}.journal.jsonl`, 'utf8').trim());
  assert.ok(newestJournal.seq > snapshot.snapshotAppliedJournalSeq,
    'post-restart journal sequence must remain monotonic above the snapshot');
});

test('first closed-history baseline indexes old rows without replay and survives restart', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-closed-watermark-')), 'state.json');
  const first = new EliteDirectWatchState(path);
  first.syncTargets([target], new Set(), BASE);
  const old = openRow({ isOpen: false, closingPrice: 0.8, createdAt: BASE - 200, closedAt: BASE - 100 });
  assert.equal(first.targets()[0].closedHistoryInitialized, false);
  first.commitClosedHydration('p1', [old], BASE + 1);

  const restarted = new EliteDirectWatchState(path);
  const stored = restarted.targets()[0];
  assert.equal(stored.closedHistoryInitialized, true);
  assert.equal(stored.closedProcessedThroughMs, BASE - 100);
  assert.deepEqual(stored.closedBoundaryIds, ['base1']);
  assert.deepEqual(closedSignalsAfterBoundary([old], target, stored.closedProcessedThroughMs, stored.closedBoundaryIds, BASE + 2), []);
});

test('closed baseline ignores 456-row history when newest timestamp group ends on page one', async () => {
  const newest = Array.from({ length: 3 }, (_, index) => openRow({
    id: `new-${index}`, baseId: `new-${index}`, isOpen: false, closedAt: BASE,
  }));
  const history = Array.from({ length: 456 }, (_, index) => openRow({
    id: `old-${index}`, baseId: `old-${index}`, isOpen: false, closedAt: BASE - index - 1,
  }));
  let calls = 0;
  const result = await establishClosedBaseline(async page => {
    calls += 1;
    const all = [...newest, ...history];
    return all.slice((page - 1) * 100, page * 100);
  }, 2);
  assert.equal(calls, 1);
  assert.equal(result.boundaryReached, true);
  assert.equal(result.boundaryReason, 'older_timestamp');
  assert.deepEqual(result.boundaryIds, ['new-0', 'new-1', 'new-2']);
  assert.equal(result.boundaryRows.length, 3);
  assert.deepEqual(closedSignalsAfterBoundary(result.boundaryRows, target, BASE, result.boundaryIds, BASE + 1), []);
});

test('closed baseline collects a newest equal-timestamp group spanning pages', async () => {
  const newest = Array.from({ length: 120 }, (_, index) => openRow({
    id: `new-${index}`, baseId: `new-${index}`, isOpen: false, closedAt: BASE,
  }));
  const older = openRow({ id: 'older', baseId: 'older', isOpen: false, closedAt: BASE - 1 });
  const all = [...newest, older];
  const result = await establishClosedBaseline(
    async page => all.slice((page - 1) * 100, page * 100), 2,
  );
  assert.equal(result.pagesFetched, 2);
  assert.equal(result.boundaryReached, true);
  assert.equal(result.boundaryReason, 'older_timestamp');
  assert.equal(result.boundaryIds.length, 120);
  assert.equal(result.boundaryRows.length, 120);
});

test('closed baseline reports overflow and cannot commit an incomplete newest group', async () => {
  const newest = Array.from({ length: 250 }, (_, index) => openRow({
    id: `new-${index}`, baseId: `new-${index}`, isOpen: false, closedAt: BASE,
  }));
  const result = await establishClosedBaseline(
    async page => newest.slice((page - 1) * 100, page * 100), 2,
  );
  assert.equal(result.pagesFetched, 2);
  assert.equal(result.boundaryReached, false);
  assert.equal(result.overflow, true);
  assert.equal(result.boundaryIds.length, 200);
});

test('within-page newest-first reversal fails closed without baseline watermark commit', async () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-ordering-within-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  const before = state.targets()[0];
  const rows = [
    openRow({ baseId: 'newer', isOpen: false, closedAt: BASE + 2 }),
    openRow({ baseId: 'older', isOpen: false, closedAt: BASE }),
    openRow({ baseId: 'reversed', isOpen: false, closedAt: BASE + 1 }),
  ];
  const result = await establishClosedBaseline(async () => rows, 2, 100);
  assert.equal(result.orderingViolation?.kind, 'within_page_reversal');
  assert.equal(result.boundaryReached, false);
  assert.equal(result.overflow, true);
  if (result.boundaryReached) state.commitClosedHydration('p1', result.boundaryRows, BASE + 3);
  assert.equal(state.targets()[0].closedHistoryInitialized, before.closedHistoryInitialized);
  assert.equal(state.targets()[0].closedProcessedThroughMs, before.closedProcessedThroughMs);
});

test('cross-page newest-first reversal fails closed without baseline watermark commit', async () => {
  const first = Array.from({ length: 100 }, (_, index) => openRow({
    baseId: `page-1-${index}`, isOpen: false, closedAt: BASE + 100,
  }));
  const second = [openRow({ baseId: 'page-2-newer', isOpen: false, closedAt: BASE + 101 })];
  assert.equal(validateClosedPageOrdering(first, 1, null).violation, null);
  const result = await establishClosedBaseline(async page => page === 1 ? first : second, 2, 100);
  assert.equal(result.orderingViolation?.kind, 'cross_page_reversal');
  assert.equal(result.boundaryReached, false);
  assert.equal(result.overflow, true);
});

test('sanitized three-page runtime probes document newest-first ordering only as an observation', () => {
  const fixture = JSON.parse(readFileSync(
    new URL('../../test/fixtures/invo-closed-ordering-observation.json', import.meta.url),
    'utf8',
  ));
  assert.equal(fixture._evidence.kind, 'sanitized_runtime_observation');
  assert.match(fixture._evidence.limitation, /not a permanent API contract/);
  assert.equal(fixture.portfolios.length, 2);
  for (const portfolio of fixture.portfolios) {
    assert.equal(portfolio.pages.length, 3);
    let priorLast: number | null = null;
    for (const page of portfolio.pages) {
      const first = Date.parse(page.first);
      const last = Date.parse(page.last);
      assert.ok(first >= last);
      if (priorLast != null) assert.ok(first <= priorLast);
      priorLast = last;
    }
  }
});

test('empty closed baseline persists safely and later first close is prospective', async () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-empty-closed-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  const baseline = await establishClosedBaseline(async () => [], 2);
  assert.equal(baseline.boundaryReached, true);
  state.commitClosedHydration('p1', baseline.boundaryRows, BASE);

  const restarted = new EliteDirectWatchState(path);
  const stored = restarted.targets()[0];
  assert.equal(stored.closedHistoryInitialized, true);
  assert.equal(stored.closedProcessedThroughMs, 0);
  assert.deepEqual(stored.closedBoundaryIds, []);
  const firstClose = openRow({ isOpen: false, closingPrice: 0.8, closedAt: BASE + 1 });
  assert.equal(closedSignalsAfterBoundary([firstClose], target, stored.closedProcessedThroughMs, [], BASE + 2).length, 1);
});

test('equal-timestamp feed/direct boundary identity is not counted twice', () => {
  const first = openRow({ isOpen: false, closingPrice: 0.8, closedAt: BASE + 100 });
  const duplicate = { ...first, id: 'different-surface-row-id' };
  assert.deepEqual(closedSignalsAfterBoundary([duplicate], target, BASE + 100, ['base1'], BASE + 200), []);
});

test('closed polling is fair for 45 targets and request math stays bounded', () => {
  const rows = Array.from({ length: 45 }, (_, index) => ({
    ...target, portfolioId: `p${String(index).padStart(2, '0')}`,
    baselineAtMs: BASE, processedThroughMs: BASE, selectorInitialized: true,
    lastSelectorUpdatedAtMs: BASE, lastFallbackPollAtMs: BASE,
    closedHistoryInitialized: true, closedProcessedThroughMs: BASE,
    closedBoundaryIds: [], lastClosedPollAtMs: BASE - 60_000 - index,
  }));
  const served = new Set<string>();
  for (let scan = 0; scan < 15; scan += 1) {
    const plan = planClosedHydrations(rows, BASE + scan * 3_000, 60_000, 3);
    for (const item of plan) {
      served.add(item.target.portfolioId);
      item.target.lastClosedPollAtMs = BASE + scan * 3_000;
    }
  }
  assert.equal(served.size, 45);
  assert.equal(15 * 3_000, 45_000);
  assert.equal(4 + 8 * 3 + 3 * 2, 34,
    'four selectors plus paginated open/drain and closed hydration is at most 34 requests per scan');
});

test('equal-timestamp closed boundary spanning 100 rows is not proven by unrelated rows', () => {
  const boundaryIds = new Set(['stored-boundary']);
  const encountered = new Set<string>();
  const sameTime = (id: string) => openRow({ id, baseId: id, isOpen: false, closingPrice: 0.8, closedAt: BASE });
  const firstPage = Array.from({ length: 100 }, (_, index) => sameTime(`new-${index}`));
  const secondPage = [
    ...Array.from({ length: 20 }, (_, index) => sameTime(`new-later-${index}`)),
    sameTime('stored-boundary'),
  ];

  assert.deepEqual(closedBoundaryProof(firstPage, BASE, boundaryIds, encountered, 100), { reached: false, reason: null });
  const unseen = closedSignalsAfterBoundary([...firstPage, ...secondPage], target, BASE, [...boundaryIds], BASE + 1);
  assert.equal(unseen.length, 120);
  assert.deepEqual(closedBoundaryProof(secondPage, BASE, boundaryIds, encountered, 100), {
    reached: true, reason: 'endpoint_exhausted',
  });
});

test('old boundary id on page one cannot hide new equal-time rows on page two', () => {
  const sameTime = (id: string) => openRow({ id, baseId: id, isOpen: false, closingPrice: 0.8,
    createdAt: BASE - 100, updatedAt: BASE, closedAt: BASE });
  const encountered = new Set<string>();
  const first = [sameTime('old'), ...Array.from({ length: 99 }, (_, i) => sameTime(`new-a-${i}`))];
  const second = Array.from({ length: 100 }, (_, i) => sameTime(`new-b-${i}`));
  const third = [openRow({ id: 'older', baseId: 'older', isOpen: false, closedAt: BASE - 1 })];
  assert.equal(closedBoundaryProof(first, BASE, new Set(['old']), encountered, 100).reached, false);
  assert.equal(closedBoundaryProof(second, BASE, new Set(['old']), encountered, 100).reached, false);
  assert.deepEqual(closedBoundaryProof(third, BASE, new Set(['old']), encountered, 100), { reached: true, reason: 'older_timestamp' });
  assert.equal(closedSignalsAfterBoundary([...first, ...second, ...third], target, BASE, ['old'], BASE + 1).length, 199);
});

test('open pagination observes page two and fails closed on overflow or ordering reversal', async () => {
  const rows = Array.from({ length: 101 }, (_, i) => openRow({ id: `i${i}`, baseId: `b${i}`, updatedAt: BASE + 200 - i }));
  const complete = await fetchCompleteOpenInvestments(async page => rows.slice((page - 1) * 100, page * 100), 3);
  assert.equal(complete.complete, true);
  assert.equal(complete.rows.length, 101);
  assert.equal(signalsFromDirectInvestments(complete.rows, [], target, BASE, BASE + 300).some(s => s.sourceBaseId === 'b100'), true);
  const overflow = await fetchCompleteOpenInvestments(async () => rows.slice(0, 100), 2);
  assert.equal(overflow.complete, false);
  assert.equal(overflow.overflow, true);
  const reversed = await fetchCompleteOpenInvestments(async () => [rows[1], rows[0]], 2);
  assert.equal(reversed.complete, false);
  assert.equal(reversed.orderingViolation?.kind, 'within_page_reversal');
});

test('stale candidate state retains targets; authoritative demotion retires and drains before deletion', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-retire-')), 'state.json');
  let state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  state.syncTargets([], new Set(), BASE + 1, false);
  assert.equal(state.targets()[0].lifecycle, 'ENROLLING');
  retireTarget(state, BASE + 10, 100);
  assert.equal(state.targets()[0].lifecycle, 'RETIRING');
  assert.equal(planDirectHydrations(state.targets(), new Map(), BASE + 20, 1, 10).length, 0,
    'OPEN drain waits until CLOSED cursor exists');
  state.initializeRetirementDrain('p1');
  assert.equal(planDirectHydrations(state.targets(), new Map(), BASE + 20, 1, 10)[0]?.reason,
    'retirement_open_drain');
  const close = openRow({ isOpen: false, closingPrice: 0.8, createdAt: BASE + 1,
    closedAt: BASE + 11, updatedAt: BASE + 11 });
  const retiring = state.targets()[0];
  assert.equal(closedSignalsAfterBoundary(
    [close], target, retiring.closedProcessedThroughMs, retiring.closedBoundaryIds, BASE + 20,
  ).length, 1, 'closed-only roundtrip after selection remains observable after demotion');
  state.commitRetirementOpenPoll('p1', [], BASE + 120);
  state.commitClosedHydration('p1', [close], BASE + 120);
  state.commitRetirementOpenPoll('p1', [], BASE + 130);
  state.commitClosedHydration('p1', [close], BASE + 130);
  state = new EliteDirectWatchState(path);
  state.syncTargets([], new Set(), BASE + 200, true, 100, new Set(['p1']));
  assert.equal(state.targets().length, 0);
});

test('active target absent from a fresh bounded cycle remains active', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-bounded-absence-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  state.commitClosedHydration('p1', [], BASE + 1);
  state.syncTargets([], new Set(), BASE + 10, true, 1, new Set());
  assert.equal(state.targets()[0].lifecycle, 'ENROLLING');
  assert.equal(planDirectHydrations(state.targets(), new Map(), BASE + 20, 1, 1).length, 1);
  assert.equal(planClosedHydrations(state.targets(), BASE + 20, 1, 1).length, 1);
});

test('pre-demotion open blocks deletion and drain proofs survive restart', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-retirement-open-')), 'state.json');
  let state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  retireTarget(state, BASE + 10, 10);
  state.commitRetirementOpenPoll('p1', [openRow({ createdAt: BASE + 1 })], BASE + 30);
  state.commitClosedHydration('p1', [], BASE + 30);
  state.commitClosedHydration('p1', [], BASE + 40);
  state.syncTargets([], new Set(), BASE + 50, true, 10, new Set(['p1']));
  assert.equal(state.targets().length, 1, 'a relevant pre-demotion source position remains open');
  assert.equal(state.targets()[0].retirementRelevantOpenCount, 1);

  state.commitRetirementOpenPoll('p1', [openRow({ createdAt: BASE + 11 })], BASE + 60);
  state = new EliteDirectWatchState(path);
  assert.equal(state.targets()[0].retirementOpenEmptyProofs, 1, 'post-demotion opens do not block drain');
  state.commitRetirementOpenPoll('p1', [], BASE + 70);
  state.syncTargets([], new Set(), BASE + 80, true, 10, new Set(['p1']));
  assert.equal(state.targets().length, 0);
});

test('retirement drain proof excludes pre-selection rows and fails closed on unknown createdAt', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-retirement-relevance-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  retireTarget(state, BASE + 10, 10);
  state.commitRetirementOpenPoll('p1', [openRow({ createdAt: BASE - 1 })], BASE + 30);
  assert.equal(state.targets()[0].retirementRelevantOpenCount, 0);
  state.commitRetirementOpenPoll('p1', [openRow({ createdAt: 'invalid' })], BASE + 40);
  assert.equal(state.targets()[0].retirementRelevantOpenCount, 1,
    'unknown lifecycle provenance prevents a false empty drain proof');
});

test('retiring hydration executes pre-demotion opens and rejects post-demotion opens', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-retirement-classify-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  retireTarget(state, BASE + 200, 100);
  const retiring = state.targets()[0];
  const dispositions = retiringOpenDispositions([
    openRow({ id: 'pre', baseId: 'pre', createdAt: BASE + 100, updatedAt: BASE + 100 }),
    openRow({ id: 'post', baseId: 'post', createdAt: BASE + 201, updatedAt: BASE + 201 }),
  ], retiring, BASE + 210, retirementLifecycle());
  assert.deepEqual(dispositions.map(row => [row.kind === 'invalid_lifecycle_open' ? row.sourceBaseId : row.signal.sourceBaseId, row.kind]), [
    ['pre', 'execute'],
    ['post', 'post_demotion_open_ignored'],
  ]);
  state.commitRetirementOpenPoll('p1', [
    openRow({ id: 'pre', baseId: 'pre', createdAt: BASE + 100 }),
    openRow({ id: 'post', baseId: 'post', createdAt: BASE + 201 }),
  ], BASE + 210);
  assert.equal(state.targets()[0].retirementRelevantOpenCount, 1,
    'all currently-open pre-demotion rows count for deletion proof regardless of execution');
  const first = dispositions[0];
  assert.notEqual(first.kind, 'invalid_lifecycle_open');
  if (first.kind === 'invalid_lifecycle_open') return;
  const pre = first.signal;
  assert.equal(isMissedPreDemotionOpen(pre, 'elite_direct:retirement_open_drain', BASE + 210, 200), false,
    'a fresh first observation remains executable');
  assert.equal(isMissedPreDemotionOpen(pre, 'elite_direct:retirement_open_drain', BASE + 401, 200), true,
    'a stale first observation is explicit missed evidence, never a fabricated open');
});

test('retiring increases require an owned pre-demotion lifecycle', () => {
  const stored = {
    ...target, lifecycle: 'RETIRING' as const, retiredAtMs: BASE + 200, retireAfterMs: BASE + 300,
    baselineAtMs: BASE, processedThroughMs: BASE + 100, selectorInitialized: true,
    lastSelectorUpdatedAtMs: BASE, lastFallbackPollAtMs: BASE,
    lastRetirementOpenPollAtMs: 0, retirementRelevantOpenCount: null,
    retirementOpenEmptyProofs: 0, retirementClosedProofs: 0,
    closedHistoryInitialized: true, closedProcessedThroughMs: BASE,
    closedBoundaryIds: [], lastClosedPollAtMs: BASE,
  };
  const increase = openRow({ createdAt: BASE + 50, updatedAt: BASE + 150,
    entrySize: 3, changes: { simIncrease: true, entrySize: 2 } });
  assert.equal(retiringOpenDispositions([increase], stored, BASE + 220,
    retirementLifecycle({ isManagedSource: () => true }))[0].kind, 'execute');
  assert.equal(retiringOpenDispositions([increase], stored, BASE + 220,
    retirementLifecycle({ hasObservedOpen: () => true }))[0].kind, 'unowned_increase_ignored',
    'an unowned increase cannot create exposure or duplicate its already-observed OPEN');
});

test('retiring discovery bypasses portfolio watermark exactly once without lowering it', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-retirement-delayed-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  state.commitHydration('p1', BASE + 180);
  retireTarget(state, BASE + 200, 100);
  const retiring = state.targets()[0];
  const delayed = openRow({ id: 'delayed', baseId: 'delayed', createdAt: BASE + 100, updatedAt: BASE + 100 });
  const [first] = retiringOpenDispositions([delayed], retiring, BASE + 210, retirementLifecycle());
  assert.equal(first.kind, 'execute');
  if (first.kind !== 'execute') return;
  assert.equal(first.signal.action, 'open');
  assert.equal(first.signal.sourceTimeMs, BASE + 100);

  const seen = new Set([first.signal.key, `source-event:delayed:open:${BASE + 100}`]);
  assert.deepEqual(retiringOpenDispositions([delayed], retiring, BASE + 211,
    retirementLifecycle({ hasObservedOpen: () => true, hasSeen: key => seen.has(key) })), []);
  state.commitHydration('p1', first.signal.sourceTimeMs ?? 0);
  assert.equal(state.targets()[0].processedThroughMs, BASE + 180, 'old delayed OPEN cannot lower high-water');
});

test('retiring OPEN interval and durable close dominance fail closed', () => {
  const stored = {
    ...target, lifecycle: 'RETIRING' as const, retiredAtMs: BASE + 200, retireAfterMs: BASE + 300,
    baselineAtMs: BASE, processedThroughMs: BASE + 180, selectorInitialized: true,
    lastSelectorUpdatedAtMs: BASE, lastFallbackPollAtMs: BASE,
    lastRetirementOpenPollAtMs: 0, retirementRelevantOpenCount: null,
    retirementOpenEmptyProofs: 0, retirementClosedProofs: 0,
    closedHistoryInitialized: true, closedProcessedThroughMs: BASE,
    closedBoundaryIds: [], lastClosedPollAtMs: BASE,
  };
  const rows = [
    openRow({ id: 'pre', baseId: 'pre', createdAt: BASE - 1, updatedAt: BASE - 1 }),
    openRow({ id: 'post', baseId: 'post', createdAt: BASE + 201, updatedAt: BASE + 201 }),
    openRow({ id: 'closed', baseId: 'closed', createdAt: BASE + 100, updatedAt: BASE + 100 }),
    openRow({ id: 'missing-time', baseId: 'missing-time', createdAt: 'not-a-date' }),
  ];
  const dispositions = retiringOpenDispositions(rows, stored, BASE + 220,
    retirementLifecycle({ hasHandledClose: id => id === 'closed' }));
  assert.deepEqual(dispositions.map(row => row.kind), [
    'pre_selection_open_ignored', 'post_demotion_open_ignored', 'invalid_lifecycle_open',
  ]);
});

test('retiring increases are causal, pre-demotion, and owned only', () => {
  const stored = {
    ...target, lifecycle: 'RETIRING' as const, retiredAtMs: BASE + 200, retireAfterMs: BASE + 300,
    baselineAtMs: BASE, processedThroughMs: BASE + 100, selectorInitialized: true,
    lastSelectorUpdatedAtMs: BASE, lastFallbackPollAtMs: BASE,
    lastRetirementOpenPollAtMs: 0, retirementRelevantOpenCount: null,
    retirementOpenEmptyProofs: 0, retirementClosedProofs: 0,
    closedHistoryInitialized: true, closedProcessedThroughMs: BASE,
    closedBoundaryIds: [], lastClosedPollAtMs: BASE,
  };
  const lifecycle = retirementLifecycle({ isManagedSource: () => true });
  const stale = openRow({ createdAt: BASE + 50, updatedAt: BASE + 100, changes: { simIncrease: true, entrySize: 1 } });
  const post = openRow({ createdAt: BASE + 50, updatedAt: BASE + 201, changes: { simIncrease: true, entrySize: 1 } });
  assert.deepEqual(retiringOpenDispositions([stale, post], stored, BASE + 220, lifecycle), []);
});

test('reactivation mutates the canonical metadata object and clears stale drain proofs immediately', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-reactivate-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  retireTarget(state, BASE + 10, 10);
  state.initializeRetirementDrain('p1');
  state.commitRetirementOpenPoll('p1', [], BASE + 30);
  state.commitClosedHydration('p1', [], BASE + 30);
  const changed = { ...target, ownerId: 'owner-new', username: 'renamed' };
  state.syncTargets([changed], new Set(), BASE + 31, true, 10, new Set());
  const reactivated = state.targets()[0];
  assert.equal(reactivated.lifecycle, 'ENROLLING');
  assert.equal(reactivated.admittedAtMs, null);
  assert.equal(reactivated.ownerId, 'owner-new');
  assert.equal(reactivated.username, 'renamed');
  assert.equal(reactivated.retiredAtMs, null);
  assert.equal(reactivated.retirementOpenEmptyProofs, 0);
  assert.equal(reactivated.retirementClosedProofs, 0);
  assert.equal(planDirectHydrations(state.targets(), new Map([['p1', BASE + 32]]), BASE + 32, 1_000, 1)[0]?.reason,
    'selector_change', 'reactivated target is eligible for open hydration in the same scan');
  state.syncTargets([changed], new Set(), BASE + 1_000, true, 10, new Set());
  assert.equal(state.targets().length, 1, 'stale retirement proof cannot delete a reactivated target');
});

test('open overflow cannot create a retirement empty proof', async () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-retirement-overflow-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  retireTarget(state, BASE + 1, 1);
  const overflow = await fetchCompleteOpenInvestments(async () => Array.from({ length: 100 }, (_, i) =>
    openRow({ id: `i${i}`, baseId: `b${i}`, updatedAt: BASE + 200 - i })), 1);
  assert.equal(overflow.complete, false);
  assert.equal(state.targets()[0].retirementOpenEmptyProofs, 0, 'caller must not commit incomplete polls');
});

test('owned retiring target is never deleted after grace and drain proof', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-owned-retire-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  retireTarget(state, BASE + 1, 1, new Set(['p1']));
  const close = openRow({ isOpen: false, closedAt: BASE + 2 });
  state.commitClosedHydration('p1', [close], BASE + 2);
  state.commitClosedHydration('p1', [close], BASE + 3);
  state.commitRetirementOpenPoll('p1', [], BASE + 3);
  state.commitRetirementOpenPoll('p1', [], BASE + 4);
  state.syncTargets([], new Set(['p1']), BASE + 10, true, 1, new Set(['p1']));
  assert.equal(state.targets().length, 1);
});

test('closed watermark advances only after pagination boundary proof', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-closed-proof-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE - 1);
  state.commitClosedHydration('p1', [openRow({ baseId: 'old', isOpen: false, closedAt: BASE })], BASE);
  const before = state.targets()[0];
  const fullUnprovenPage = Array.from({ length: 100 }, (_, index) => openRow({
    id: `new-${index}`, baseId: `new-${index}`, isOpen: false, closedAt: BASE + 1,
  }));
  const proof = closedBoundaryProof(fullUnprovenPage, BASE, new Set(['old']), new Set(), 100);
  if (proof.reached) state.commitClosedHydration('p1', fullUnprovenPage, BASE + 2);
  assert.equal(state.targets()[0].closedProcessedThroughMs, before.closedProcessedThroughMs);
  assert.deepEqual(state.targets()[0].closedBoundaryIds, before.closedBoundaryIds);
});

test('persistent first-target 500 still gives all later 44 targets bounded attempts and does not starve closed phase', async () => {
  const deadlines = Array.from({ length: 45 }, () => 0);
  const attempted: number[] = [];
  let failures = 0;
  for (let scan = 0; scan < 6; scan += 1) {
    const plan = Array.from({ length: 45 }, (_, index) => index)
      .sort((a, b) => deadlines[a] - deadlines[b] || a - b)
      .slice(0, 8);
    const open = await runIsolatedHydrations(plan, item => {
      attempted.push(item);
      deadlines[item] = BASE + scan;
    }, async item => {
      if (item === 0) throw Object.assign(new Error('persistent'), { status: 500 });
    });
    failures += open.failed.length;
  }
  let closedAttempted = 0;
  const closed = await runIsolatedHydrations([0, 1, 2], () => { closedAttempted += 1; }, async () => {});
  assert.equal(failures, 2, 'target zero is retried only after all peers receive their first attempt');
  assert.equal(new Set(attempted).size, 45);
  assert.equal(closed.attempted.length, 3);
  assert.equal(closedAttempted, 3);
});

test('three permanent closed-baseline failures cannot starve the later 42 targets', async () => {
  const rows = Array.from({ length: 45 }, (_, index) => ({
    ...target, portfolioId: `p${String(index).padStart(2, '0')}`,
    baselineAtMs: BASE, processedThroughMs: BASE, selectorInitialized: true,
    lastSelectorUpdatedAtMs: BASE, lastFallbackPollAtMs: BASE,
    closedHistoryInitialized: false, closedProcessedThroughMs: 0,
    closedBoundaryIds: [], lastClosedPollAtMs: 0,
  }));
  const attempted = new Set<string>();
  for (let scan = 0; scan < 15; scan += 1) {
    const at = BASE + scan * 60_000;
    const plan = planClosedHydrations(rows, at, 60_000, 3);
    await runIsolatedHydrations(plan, item => {
      attempted.add(item.target.portfolioId);
      item.target.lastClosedPollAtMs = at;
    }, async item => {
      if (['p00', 'p01', 'p02'].includes(item.target.portfolioId)) throw new Error('permanent baseline failure');
    });
  }
  assert.equal(attempted.size, 45);
});

test('429 stops bounded work, reports skipped targets, and recorded attempt rotates next scan', async () => {
  const deadlines = [0, 0, 0];
  const plan = () => [0, 1, 2].sort((a, b) => deadlines[a] - deadlines[b] || a - b);
  const first = await runIsolatedHydrations(plan(), item => { deadlines[item] = BASE; }, async item => {
    if (item === 0) throw Object.assign(new Error('quota'), { status: 429 });
  });
  assert.equal(first.rateLimited, true);
  assert.deepEqual(first.skippedAfterRateLimit, [1, 2]);
  assert.deepEqual(plan(), [1, 2, 0], 'failed target rotates behind unattempted peers after cooldown');
});

test('authoritative negative evidence requires distinct observations spanning grace', () => {
  const state = new EliteDirectWatchState(join(mkdtempSync(join(tmpdir(), 'elite-negative-grace-')), 'state.json'));
  state.syncTargets([target], new Set(), BASE);
  state.syncTargets([], new Set(), BASE + 10, true, 100, new Set(['p1']), 48, 2, 600_000, BASE + 10);
  assert.equal(state.targets()[0].lifecycle, 'MISSING_GRACE');
  assert.equal(state.targets()[0].negativeEvidenceCount, 1);
  state.syncTargets([], new Set(), BASE + 20, true, 100, new Set(['p1']), 48, 2, 600_000, BASE + 10);
  assert.equal(state.targets()[0].negativeEvidenceCount, 1, 'duplicate observation timestamp is not independent evidence');
  state.syncTargets([], new Set(), BASE + 600_010, true, 100, new Set(['p1']), 48, 2, 600_000, BASE + 600_010);
  assert.equal(state.targets()[0].lifecycle, 'RETIRING');
  assert.equal(state.targets()[0].negativeEvidenceCount, 2);
});

test('elite reappearance from grace or retirement preserves every causal watermark', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-reappearance-watermarks-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  state.commitHydration('p1', BASE + 50, BASE + 40);
  state.commitClosedHydration('p1', [openRow({ isOpen: false, baseId: 'boundary', closedAt: BASE + 60 })], BASE + 61);
  const expected = state.targets()[0];
  state.syncTargets([], new Set(), BASE + 70, true, 100, new Set(['p1']), 48, 2, 600_000, BASE + 70);
  state.syncTargets([target], new Set(), BASE + 80, true);
  for (const key of ['baselineAtMs', 'processedThroughMs', 'closedProcessedThroughMs', 'closedBoundaryIds',
    'selectorInitialized', 'lastSelectorUpdatedAtMs'] as const) assert.deepEqual(state.targets()[0][key], expected[key]);
  retireTarget(state, BASE + 100, 100);
  state.syncTargets([target], new Set(), BASE + 101, true);
  for (const key of ['baselineAtMs', 'processedThroughMs', 'closedProcessedThroughMs', 'closedBoundaryIds',
    'selectorInitialized', 'lastSelectorUpdatedAtMs'] as const) assert.deepEqual(state.targets()[0][key], expected[key]);
});

test('resident cap defers deterministically, never evicts, and later enrolls prospectively', () => {
  const state = new EliteDirectWatchState(join(mkdtempSync(join(tmpdir(), 'elite-cap-')), 'state.json'));
  const p2 = { ...target, portfolioId: 'p2' };
  state.syncTargets([target, p2], new Set(), BASE, true, 100, new Set(), 1);
  assert.deepEqual(state.targets().map(row => row.portfolioId), ['p1']);
  assert.deepEqual(state.deferredAdmissions().map(row => row.portfolioId), ['p2']);
  retireTarget(state, BASE + 10, 1);
  state.initializeRetirementDrain('p1');
  state.commitRetirementOpenPoll('p1', [], BASE + 20);
  state.commitClosedHydration('p1', [], BASE + 20);
  state.commitRetirementOpenPoll('p1', [], BASE + 21);
  state.commitClosedHydration('p1', [], BASE + 21);
  state.syncTargets([], new Set(), BASE + 22, true, 1, new Set(['p1']), 1);
  assert.equal(state.targets().length, 0);
  assert.equal(state.tombstones().length, 1);
  state.syncTargets([p2], new Set(), BASE + 30, true, 100, new Set(), 1);
  assert.equal(state.targets()[0].portfolioId, 'p2');
  assert.equal(state.targets()[0].processedThroughMs, BASE + 30, 'deferred admission starts prospectively');
});

test('tombstone restores causal watermarks without consuming resident capacity', () => {
  const state = new EliteDirectWatchState(join(mkdtempSync(join(tmpdir(), 'elite-tombstone-')), 'state.json'));
  state.syncTargets([target], new Set(), BASE);
  state.commitHydration('p1', BASE + 50, BASE + 40);
  state.commitClosedHydration('p1', [openRow({ isOpen: false, baseId: 'boundary', closedAt: BASE + 60 })], BASE + 61);
  const before = state.targets()[0];
  retireTarget(state, BASE + 100, 1);
  state.commitRetirementOpenPoll('p1', [], BASE + 102);
  state.commitClosedHydration('p1', [], BASE + 102);
  state.commitRetirementOpenPoll('p1', [], BASE + 103);
  state.commitClosedHydration('p1', [], BASE + 103);
  state.syncTargets([], new Set(), BASE + 104, true, 1, new Set(['p1']));
  assert.equal(state.status().targetCount, 0);
  assert.equal(state.status().tombstoneCount, 1);
  state.syncTargets([target], new Set(), BASE + 200, true, 1, new Set(), 1);
  const restored = state.targets()[0];
  assert.equal(restored.baselineAtMs, before.baselineAtMs);
  assert.equal(restored.processedThroughMs, BASE + 200);
  assert.equal(restored.closedProcessedThroughMs, BASE + 200);
  assert.deepEqual(restored.closedBoundaryIds, []);
});

test('default capacity proof sustains hard floor 16 at timeout/page/concurrency/rate bounds', () => {
  const defaults = validateDirectWatchCapacity({ residentCap: 48, scanMs: 3_000,
    maxOpenHydratesPerScan: 24, openPollMs: 18_000,
    maxClosedHydratesPerScan: 24, closedPollMs: 60_000,
    requestTimeoutMs: 2_000, maxAttemptsPerPage: 2,
    openMaxPages: 3, closedMaxPages: 2, fixedOverheadMs: 2_000,
    concurrency: 16, requestBudgetPerSecond: 12, requestBudgetBurst: 32,
    fixedReserveRequestsPerSecond: 4 });
  assert.equal(defaults.sustainableOpenTargetCeiling, 16);
  assert.ok(defaults.sustainableClosedTargetCeiling >= 16);
  assert.equal(defaults.hardProvenResidentCap, 16);
  assert.equal(defaults.worstCaseOpenSweepMsAtCap, 14_000);
  assert.throws(() => validateDirectWatchCapacity({ residentCap: 48, scanMs: 3_000,
    maxOpenHydratesPerScan: 8, openPollMs: 18_000,
    maxClosedHydratesPerScan: 3, closedPollMs: 60_000,
    requestTimeoutMs: 2_000, maxAttemptsPerPage: 2,
    openMaxPages: 3, closedMaxPages: 2, fixedOverheadMs: 18_000,
    concurrency: 1, requestBudgetPerSecond: 12, requestBudgetBurst: 32,
    fixedReserveRequestsPerSecond: 4 }),
  /cannot prove even one/);
});

test('worst-case max-page virtual latency keeps 16 OPEN and CLOSED residents inside deadlines', () => {
  const residents = 16; const workers = 16; const timeoutMs = 4_000; const overheadMs = 2_000;
  const simulatePhase = (startMs: number, pages: number) => {
    const workerReady = Array.from({ length: workers }, () => startMs);
    for (let targetIndex = 0; targetIndex < residents; targetIndex += 1) {
      const worker = workerReady.indexOf(Math.min(...workerReady));
      workerReady[worker] += pages * timeoutMs;
    }
    return Math.max(...workerReady) + overheadMs;
  };
  const openCompletedAtMs = simulatePhase(0, 3);
  const closedCompletedAtMs = simulatePhase(openCompletedAtMs - overheadMs, 2);
  assert.equal(openCompletedAtMs, 14_000);
  assert.ok(openCompletedAtMs <= 18_000);
  assert.ok(closedCompletedAtMs <= 60_000,
    'even a simultaneous CLOSED sweep queued behind worst-case OPEN completes by its deadline');
  assert.ok(16 * (6 / 18 + 4 / 60) <= 8,
    'steady-state max-page demand fits the post-reserve direct request rate');
});

test('deadline scheduler interleaves overdue CLOSED ahead of newer OPEN work', () => {
  const stored = { ...target, baselineAtMs: BASE, processedThroughMs: BASE, selectorInitialized: true,
    lastSelectorUpdatedAtMs: BASE, lastFallbackPollAtMs: BASE + 50_000,
    closedHistoryInitialized: true, closedProcessedThroughMs: BASE, closedBoundaryIds: [],
    lastClosedPollAtMs: BASE, lifecycle: 'ACTIVE' as const };
  const open = [{ target: stored, selectorUpdatedAtMs: null, reason: 'periodic_direct_poll' as const }];
  const closed = [{ target: stored, reason: 'periodic_closed_poll' as const }];
  assert.equal(planDeadlineHydrations(open, closed, 18_000, 60_000)[0].phase, 'CLOSED');
});

test('bounded concurrent workers isolate failure and stop launching peers after 429', async () => {
  let active = 0; let maxActive = 0;
  const result = await runConcurrentHydrations([0, 1, 2, 3, 4], 2, () => {}, async item => {
    active += 1; maxActive = Math.max(maxActive, active);
    await new Promise(resolve => setTimeout(resolve, item === 0 ? 2 : 5));
    active -= 1;
    if (item === 0) throw Object.assign(new Error('quota'), { status: 429 });
  });
  assert.equal(maxActive, 2);
  assert.equal(result.rateLimited, true);
  assert.ok(result.skippedAfterRateLimit.length >= 2);
});

test('token bucket never exceeds burst plus configured refill and 429 pauses acquisition', async () => {
  let now = 0;
  const budget = new DirectWatchRequestBudget(2, 2, () => now, async ms => { now += ms; });
  const granted: number[] = [];
  for (let index = 0; index < 5; index += 1) { await budget.acquire(); granted.push(now); }
  assert.deepEqual(granted, [0, 0, 500, 1000, 1500]);
  budget.note429(2_500);
  await assert.rejects(() => budget.acquire(), (error: any) => error.status === 429);
});

test('v4 state migrates without resetting causal fields and fails closed above cap', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-v4-migration-')), 'state.json');
  writeFileSync(path, JSON.stringify({ version: 'lane3-elite-direct-watch-v4-20260918', targets: {
    p1: { ...target, lifecycle: 'ACTIVE', baselineAtMs: BASE, processedThroughMs: BASE + 50,
      selectorInitialized: true, lastSelectorUpdatedAtMs: BASE + 40, lastFallbackPollAtMs: BASE + 45,
      closedHistoryInitialized: true, closedProcessedThroughMs: BASE + 60,
      closedBoundaryIds: ['boundary'], lastClosedPollAtMs: BASE + 61 },
  } }));
  const state = new EliteDirectWatchState(path);
  const migrated = state.targets()[0];
  assert.equal(migrated.processedThroughMs, BASE + 50);
  assert.equal(migrated.closedProcessedThroughMs, BASE + 60);
  assert.deepEqual(migrated.closedBoundaryIds, ['boundary']);
  assert.equal(migrated.negativeEvidenceCount, 0);
  assert.doesNotThrow(() => state.assertResidentCap(1));
  assert.throws(() => state.assertResidentCap(0), /exceeding configured cap/);
});

test('rotating bounded universes stay capped and only quality ranking can drain incumbents', () => {
  const state = new EliteDirectWatchState(join(mkdtempSync(join(tmpdir(), 'elite-rotation-cap-')), 'state.json'));
  for (let cycle = 0; cycle < 20; cycle += 1) {
    const rows = Array.from({ length: 4 }, (_, offset) => ({ ...target, portfolioId: `p${cycle * 4 + offset}` }));
    state.syncTargets(rows, new Set(), BASE + cycle, true, 100, new Set(), 3);
    assert.ok(state.targets().length <= 3);
    assert.ok(state.targets().every(row => row.lifecycle === 'ENROLLING' || (
      row.lifecycle === 'RETIRING' && row.negativeEvidenceReason === 'capacity_quality_displacement'
    )));
  }
});

test('two-phase enrollment absorbs a pre-admission round trip and admits only after both baselines', () => {
  const dir = mkdtempSync(join(tmpdir(), 'elite-two-phase-'));
  const state = new EliteDirectWatchState(join(dir, 'watch.json'), join(dir, 'admissions.json'));
  state.syncTargets([target], new Set(), BASE, true, 120_000, new Set(), 1);
  assert.equal(state.targets()[0].lifecycle, 'ENROLLING');
  assert.deepEqual(JSON.parse(readFileSync(join(dir, 'admissions.json'), 'utf8')).rows, {});

  const preAdmissionClose = openRow({ isOpen: false, baseId: 'pre-admission-round-trip',
    createdAt: BASE + 1, updatedAt: BASE + 2, closedAt: BASE + 2, closingPrice: 0.8 });
  state.commitClosedHydration('p1', [preAdmissionClose], BASE + 3);
  assert.equal(state.targets()[0].closedHistoryInitialized, true);
  assert.deepEqual(JSON.parse(readFileSync(join(dir, 'admissions.json'), 'utf8')).rows, {});

  state.commitOpenBaseline('p1', [], BASE + 4);
  const admitted = state.targets()[0];
  assert.equal(admitted.lifecycle, 'ACTIVE');
  assert.equal(admitted.openHistoryInitialized, true);
  assert.equal(admitted.admittedAtMs, BASE + 4);
  state.setAdmissionHealth(true);
  assert.equal(JSON.parse(readFileSync(join(dir, 'admissions.json'), 'utf8')).rows.p1.admittedAtMs, BASE + 4);
  assert.deepEqual(signalsFromDirectInvestments([], [preAdmissionClose], admitted,
    Math.max(admitted.closedProcessedThroughMs, admitted.admittedAtMs ?? 0), BASE + 5), []);

  const postAdmissionOpen = openRow({ id: 'post', baseId: 'post', createdAt: BASE + 5, updatedAt: BASE + 5 });
  assert.equal(signalsFromDirectInvestments([postAdmissionOpen], [], admitted,
    Math.max(admitted.processedThroughMs, admitted.admittedAtMs ?? 0), BASE + 6)[0]?.sourceBaseId, 'post');
});

test('higher-score candidate displaces by safe retirement and remains waitlisted until capacity frees', () => {
  const dir = mkdtempSync(join(tmpdir(), 'elite-quality-capacity-'));
  const state = new EliteDirectWatchState(join(dir, 'watch.json'), join(dir, 'admissions.json'));
  const low = { ...target, portfolioId: 'z-low', score: 10 };
  const high = { ...target, portfolioId: 'a-high', score: 99 };
  state.syncTargets([low], new Set(), BASE, true, 1, new Set(), 1);
  state.commitClosedHydration(low.portfolioId, [], BASE + 1);
  state.commitOpenBaseline(low.portfolioId, [], BASE + 2);
  state.syncTargets([low, high], new Set(), BASE + 3, true, 1, new Set(), 1);
  assert.equal(state.targets()[0].lifecycle, 'RETIRING');
  assert.equal(state.targets()[0].negativeEvidenceReason, 'capacity_quality_displacement');
  assert.equal(state.deferredAdmissions()[0].portfolioId, high.portfolioId);
  assert.deepEqual(JSON.parse(readFileSync(join(dir, 'admissions.json'), 'utf8')).rows, {});

  state.commitRetirementOpenPoll(low.portfolioId, [], BASE + 5);
  state.commitClosedHydration(low.portfolioId, [], BASE + 5);
  state.commitRetirementOpenPoll(low.portfolioId, [], BASE + 6);
  state.commitClosedHydration(low.portfolioId, [], BASE + 6);
  state.syncTargets([high], new Set(), BASE + 7, true, 1, new Set(), 1);
  state.syncTargets([high], new Set(), BASE + 8, true, 1, new Set(), 1);
  assert.equal(state.targets()[0].portfolioId, high.portfolioId);
  assert.equal(state.targets()[0].lifecycle, 'ENROLLING');
});

test('rotating waitlists and tombstones keep durable cardinality and bytes bounded', () => {
  const dir = mkdtempSync(join(tmpdir(), 'elite-bounded-state-'));
  const path = join(dir, 'watch.json');
  const state = new EliteDirectWatchState(path);
  for (let cycle = 0; cycle < 400; cycle += 1) {
    const candidates = Array.from({ length: 20 }, (_, offset) => ({ ...target,
      portfolioId: `p-${cycle}-${offset}`, score: 1_000 - offset }));
    state.syncTargets(candidates, new Set(), BASE + cycle, true, 1, new Set(), 2);
  }
  const status = state.status();
  assert.ok(status.targetCount <= 2);
  assert.ok(status.deferredAdmissionCount <= 20);
  assert.ok(status.tombstoneCount <= 256);
  assert.ok(status.durableStateCardinality <= 278);
  assert.ok(status.serializedStateBytes < 256 * 1024);
});

test('unknown, malformed, and corrupt journal state fail closed loudly', () => {
  for (const [name, content] of [
    ['unknown', JSON.stringify({ version: 'future-version', targets: {} })],
    ['malformed', '{'],
  ]) {
    const path = join(mkdtempSync(join(tmpdir(), `elite-corrupt-${name}-`)), 'watch.json');
    writeFileSync(path, content);
    assert.throws(() => new EliteDirectWatchState(path), /state load failed closed/);
  }
  const dir = mkdtempSync(join(tmpdir(), 'elite-corrupt-journal-'));
  const path = join(dir, 'watch.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  writeFileSync(`${path}.journal.jsonl`, '{bad\n');
  assert.throws(() => new EliteDirectWatchState(path), /state load failed closed/);
});
