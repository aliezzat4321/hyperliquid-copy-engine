import assert from 'node:assert/strict';
import test from 'node:test';
import { planClosedHydrations, planDirectHydrations, type EliteDirectTarget } from '../src/elite-direct-watch.js';

const BASE = 1_780_000_000_000;
const target: EliteDirectTarget = {
  portfolioId: 'p00', ownerId: 'o', username: 'elite', sourceFilter: 'trending', score: 100,
};

function residents(count: number) {
  return Array.from({ length: count }, (_, index) => ({
    ...target,
    portfolioId: 'p' + String(index).padStart(2, '0'),
    lifecycle: 'ACTIVE' as const,
    baselineAtMs: BASE,
    processedThroughMs: BASE,
    selectorInitialized: true,
    lastSelectorUpdatedAtMs: BASE,
    lastFallbackPollAtMs: BASE,
    closedHistoryInitialized: true,
    closedProcessedThroughMs: BASE,
    closedBoundaryIds: [],
    lastClosedPollAtMs: BASE,
  }));
}

for (const count of [2, 5, 10, 20, 42]) {
  test(`event-driven coverage scales to ${count} residents without fast-polling idle portfolios`, () => {
    const rows = residents(count);
    const hot = new Set([rows[0].portfolioId, rows[rows.length - 1].portfolioId]);
    const fast = planDirectHydrations(rows, new Map(), BASE + 20_000, 18_000, count, hot, 600_000);
    assert.deepEqual(fast.map(row => row.target.portfolioId), [...hot]);
    const safety = planDirectHydrations(rows, new Map(), BASE + 600_001, 18_000, count, hot, 600_000);
    assert.equal(safety.length, count);
    assert.deepEqual(safety.slice(0, 2).map(row => row.target.portfolioId), [...hot]);
  });
}


for (const count of [2, 5, 10, 20, 42]) {
  test(`event-driven closed reconciliation scales to ${count} residents without fast-polling idle portfolios`, () => {
    const rows = residents(count);
    const hot = new Set([rows[0].portfolioId, rows[rows.length - 1].portfolioId]);
    const fast = planClosedHydrations(rows, BASE + 61_000, 60_000, count, hot, 600_000);
    assert.deepEqual(fast.map(row => row.target.portfolioId), [...hot]);
    const safety = planClosedHydrations(rows, BASE + 600_001, 60_000, count, hot, 600_000);
    assert.equal(safety.length, count);
    assert.deepEqual(safety.slice(0, 2).map(row => row.target.portfolioId), [...hot]);
  });
}


test('non-owned retiring residents use idle backstop while hot retiring exposure stays fast', () => {
  const rows = residents(20).map((row, index) => ({ ...row,
    lifecycle: index < 10 ? 'RETIRING' as const : row.lifecycle,
    lastRetirementOpenPollAtMs: BASE,
  }));
  const hotRetiring = rows[0].portfolioId;
  const hot = new Set([hotRetiring]);
  const fastOpen = planDirectHydrations(rows, new Map(), BASE + 20_000, 18_000, 20, hot, 600_000);
  assert.deepEqual(fastOpen.map(row => row.target.portfolioId), [hotRetiring]);
  const fastClosed = planClosedHydrations(rows, BASE + 61_000, 60_000, 20, hot, 600_000);
  assert.equal(fastClosed.some(row => row.target.portfolioId === hotRetiring), true);
  assert.equal(fastClosed.some(row => row.target.lifecycle === 'RETIRING'
    && row.target.portfolioId !== hotRetiring), false);
  const safetyOpen = planDirectHydrations(rows, new Map(), BASE + 600_001, 18_000, 20, hot, 600_000);
  assert.equal(safetyOpen.filter(row => row.target.lifecycle === 'RETIRING').length, 10);
});
