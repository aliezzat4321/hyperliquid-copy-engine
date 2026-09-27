import assert from 'node:assert/strict';
import test from 'node:test';
import { planDirectHydrations, type EliteDirectTarget } from '../src/elite-direct-watch.js';

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
