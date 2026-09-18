import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import {
  EliteDirectWatchState,
  establishClosedBaseline,
  fetchCompleteOpenInvestments,
  closedBoundaryProof,
  closedSignalsAfterBoundary,
  loadEliteDirectTargets,
  planClosedHydrations,
  planDirectHydrations,
  runIsolatedHydrations,
  signalsFromDirectInvestments,
  unownedCloseEvidence,
  validateClosedPageOrdering,
  type EliteDirectTarget,
} from '../src/elite-direct-watch.js';
import { ELITE_SELECTOR_VERSION } from '../src/portfolio-candidates.js';

const BASE = 1_780_000_000_000;

const target: EliteDirectTarget = {
  portfolioId: 'p1', ownerId: 'o1', username: 'elite', sourceFilter: 'trending',
};

function openRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 'inv1', baseId: 'base1', baseShortId: 'short1', ticker: 'SUI',
    verifiedTrade: true, isOpen: true, directionLong: true, leverage: 7,
    entryPrice: 0.75, entrySize: 2, createdAt: BASE + 100, updatedAt: BASE + 100,
    portfolio: { id: 'p1' }, ...overrides,
  };
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
    assert.equal(watch.targets()[0]?.lifecycle, 'ACTIVE');
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

  const first = planDirectHydrations(state.targets(), new Map(), BASE, 25_000, 1);
  assert.equal(first[0].target.portfolioId, 'p1');
  state.noteFallbackPoll('p1', BASE);

  const second = planDirectHydrations(state.targets(), new Map(), BASE, 25_000, 1);
  assert.equal(second[0].target.portfolioId, 'p2');
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
    portfolioId: 'portfolio-direct', ownerId: 'owner-direct', username: 'captured-shape', sourceFilter: 'fire_moves',
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
  assert.equal(state.targets()[0].lifecycle, 'ACTIVE');
  state.syncTargets([], new Set(), BASE + 10, true, 100, new Set(['p1']));
  assert.equal(state.targets()[0].lifecycle, 'RETIRING');
  assert.equal(planDirectHydrations(state.targets(), new Map(), BASE + 20, 1, 10)[0]?.reason,
    'retirement_open_drain');
  state.initializeRetirementDrain('p1');
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
  state.syncTargets([], new Set(), BASE + 10, true, 1, new Set());
  assert.equal(state.targets()[0].lifecycle, 'ACTIVE');
});

test('pre-demotion open blocks deletion and drain proofs survive restart', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-retirement-open-')), 'state.json');
  let state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  state.syncTargets([], new Set(), BASE + 10, true, 10, new Set(['p1']));
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

test('open overflow cannot create a retirement empty proof', async () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-retirement-overflow-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  state.syncTargets([], new Set(), BASE + 1, true, 1, new Set(['p1']));
  const overflow = await fetchCompleteOpenInvestments(async () => Array.from({ length: 100 }, (_, i) =>
    openRow({ id: `i${i}`, baseId: `b${i}`, updatedAt: BASE + 200 - i })), 1);
  assert.equal(overflow.complete, false);
  assert.equal(state.targets()[0].retirementOpenEmptyProofs, 0, 'caller must not commit incomplete polls');
});

test('owned retiring target is never deleted after grace and drain proof', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'elite-owned-retire-')), 'state.json');
  const state = new EliteDirectWatchState(path);
  state.syncTargets([target], new Set(), BASE);
  state.syncTargets([], new Set(['p1']), BASE + 1, true, 1, new Set(['p1']));
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
