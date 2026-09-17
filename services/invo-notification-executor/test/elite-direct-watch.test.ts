import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import {
  EliteDirectWatchState,
  loadEliteDirectTargets,
  planDirectHydrations,
  signalsFromDirectInvestments,
  type EliteDirectTarget,
} from '../src/elite-direct-watch.js';

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
    lastObservedAtMs: BASE,
    portfolios: {
      p1: { ...target, bucket: 'ELITE_CANDIDATE' },
      p2: { portfolioId: 'p2', ownerId: 'o2', username: 'wide', sourceFilter: 'all', bucket: 'RESEARCH_WIDE' },
    },
  }));
  const fresh = loadEliteDirectTargets(path, BASE + 10, 20_000);
  assert.equal(fresh.stale, false);
  assert.deepEqual(fresh.targets, [target]);
  const stale = loadEliteDirectTargets(path, BASE + 20_001, 20_000);
  assert.equal(stale.stale, true);
  assert.deepEqual(stale.targets, []);
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
  assert.equal(restarted.targets().length, 0);
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
