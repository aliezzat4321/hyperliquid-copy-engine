import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, readFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { classifyPortfolio, PortfolioCandidateLedger } from '../src/portfolio-candidates.js';

const now = Date.parse('2026-09-16T00:00:00Z');
const old = '2025-01-01T00:00:00Z';

function portfolio(overrides: Record<string, unknown> = {}) {
  return {
    id: 'p-elite',
    name: 'Elite Portfolio',
    ownerId: 'owner-1',
    owner: { id: 'owner-1', username: 'greattrader', verified: true },
    createdAt: old,
    closedPositions: 500,
    wonPositions: 465,
    lostPositions: 35,
    winRate: 93,
    percentChange: 3000,
    currentWinStreak: 8,
    liquidated: false,
    ...overrides,
  };
}

test('elite classification rewards quality without requiring an old or huge sample', () => {
  const result = classifyPortfolio(portfolio(), now, 'trending');
  assert.ok(result);
  assert.equal(result.bucket, 'ELITE_CANDIDATE');
  assert.equal(result.portfolioId, 'p-elite');
  assert.equal(result.ownerId, 'owner-1');
  assert.equal(result.winRatePct, 93);
  assert.equal(result.closedPositions, 500);
});

test('a strong newer trader with 20-30 closes and sub-500 percent return can enter shadow research', () => {
  const result = classifyPortfolio(portfolio({
    id: 'p-new-strong',
    createdAt: '2026-09-08T00:00:00Z',
    lastTradeAt: '2026-09-15T06:00:00Z',
    closedPositions: 24,
    wonPositions: 20,
    lostPositions: 4,
    winRate: 83.3,
    percentChange: 120,
  }), now, '1W');
  assert.ok(result);
  assert.equal(result.bucket, 'ELITE_CANDIDATE');
  assert.equal(result.closedPositions, 24);
  assert.equal(result.daysActive, 8);
  assert.equal(result.percentChange, 120);
  assert.ok((result.closedPositionsPerDay ?? 0) >= 3);
  assert.ok((result.scoreBreakdown.recentActivity ?? 0) > 0);
});

test('win rate below 80 percent never enters elite shadow even with exceptional return', () => {
  const result = classifyPortfolio(portfolio({
    id: 'p-79-9',
    createdAt: '2026-08-01T00:00:00Z',
    closedPositions: 100,
    wonPositions: 79,
    lostPositions: 21,
    winRate: 79.9,
    percentChange: 5000,
  }), now, '1M');
  assert.ok(result);
  assert.notEqual(result.bucket, 'ELITE_CANDIDATE');
  assert.ok(result.reasons.includes('win_rate_below_floor'));
});

test('80 percent can qualify when the rest of the portfolio evidence is strong', () => {
  const result = classifyPortfolio(portfolio({
    id: 'p-80-floor',
    createdAt: '2026-09-01T00:00:00Z',
    lastTradeAt: '2026-09-15T12:00:00Z',
    closedPositions: 35,
    wonPositions: 28,
    lostPositions: 7,
    winRate: 80,
    percentChange: 500,
  }), now, '1M');
  assert.ok(result);
  assert.equal(result.bucket, 'ELITE_CANDIDATE');
});

test('500 percent return is rewarded strongly but is not a minimum gate', () => {
  const moderateReturn = classifyPortfolio(portfolio({
    id: 'p-120',
    createdAt: '2026-09-01T00:00:00Z',
    closedPositions: 35,
    wonPositions: 30,
    lostPositions: 5,
    winRate: 85.7,
    percentChange: 120,
  }), now, '1M');
  const idealReturn = classifyPortfolio(portfolio({
    id: 'p-500',
    createdAt: '2026-09-01T00:00:00Z',
    closedPositions: 35,
    wonPositions: 30,
    lostPositions: 5,
    winRate: 85.7,
    percentChange: 500,
  }), now, '1M');
  assert.ok(moderateReturn);
  assert.ok(idealReturn);
  assert.equal(moderateReturn.bucket, 'ELITE_CANDIDATE');
  assert.equal(idealReturn.bucket, 'ELITE_CANDIDATE');
  assert.ok(idealReturn.score > moderateReturn.score);
});

test('explicit recent activity and daily frequency add weight rather than acting as oversized hard gates', () => {
  const recent = classifyPortfolio(portfolio({
    id: 'p-recent',
    createdAt: '2026-09-02T00:00:00Z',
    lastTradeAt: '2026-09-15T18:00:00Z',
    closedPositions: 42,
    wonPositions: 36,
    lostPositions: 6,
    winRate: 85.7,
    percentChange: 180,
  }), now, '1W');
  const stale = classifyPortfolio(portfolio({
    id: 'p-stale',
    createdAt: '2026-09-02T00:00:00Z',
    lastTradeAt: '2026-08-20T00:00:00Z',
    closedPositions: 42,
    wonPositions: 36,
    lostPositions: 6,
    winRate: 85.7,
    percentChange: 180,
  }), now, '1W');
  assert.ok(recent);
  assert.ok(stale);
  assert.ok((recent.scoreBreakdown.dailyFrequency ?? 0) > 0);
  assert.ok((recent.scoreBreakdown.recentActivity ?? 0) > (stale.scoreBreakdown.recentActivity ?? 0));
  assert.ok(recent.score > stale.score);
});

test('multiple portfolios from the same owner can qualify when each independently makes the cut', () => {
  const dir = mkdtempSync(join(tmpdir(), 'portfolio-ledger-'));
  const state = join(dir, 'state.json');
  const snapshots = join(dir, 'snapshots.jsonl');
  const ledger = new PortfolioCandidateLedger(state, snapshots);
  ledger.observe([
    portfolio({ id: 'p-good-a' }),
    portfolio({ id: 'p-good-b', name: 'Second Good Portfolio', closedPositions: 80, wonPositions: 65, lostPositions: 15, winRate: 81.25, percentChange: 650 }),
    portfolio({ id: 'p-bad', name: 'Bad Portfolio', closedPositions: 200, wonPositions: 70, lostPositions: 130, winRate: 35, percentChange: -60 }),
  ], 'trending', now);

  assert.equal(ledger.get('p-good-a')?.bucket, 'ELITE_CANDIDATE');
  assert.equal(ledger.get('p-good-b')?.bucket, 'ELITE_CANDIDATE');
  assert.equal(ledger.get('p-bad')?.bucket, 'REJECTED_DEMOTED');
  assert.equal(ledger.report().uniqueOwnerCount, 1);
  assert.equal(ledger.report().uniquePortfolioCount, 3);
  assert.deepEqual(new Set(ledger.report().elitePortfolioIds), new Set(['p-good-a', 'p-good-b']));
});

test('high return with too little history stays out of elite', () => {
  const result = classifyPortfolio(portfolio({ id: 'p-sparse', closedPositions: 12, wonPositions: 11, lostPositions: 1, winRate: 91.7, percentChange: 1400 }), now, 'all');
  assert.ok(result);
  assert.equal(result.bucket, 'SPARSE_HIGH_RETURN');
});

test('liquidated portfolios are rejected regardless of headline return', () => {
  const result = classifyPortfolio(portfolio({ id: 'p-liquidated', liquidated: true, winRate: 99, percentChange: 50_000 }), now, 'trending');
  assert.ok(result);
  assert.equal(result.bucket, 'REJECTED_DEMOTED');
  assert.ok(result.reasons.includes('liquidated'));
});

test('candidate snapshots are immutable and persist portfolio ids separately', () => {
  const dir = mkdtempSync(join(tmpdir(), 'portfolio-ledger-'));
  const state = join(dir, 'state.json');
  const snapshots = join(dir, 'snapshots.jsonl');
  const ledger = new PortfolioCandidateLedger(state, snapshots);
  ledger.observe([portfolio({ id: 'p-1' })], 'trending', now);
  ledger.observe([portfolio({ id: 'p-1', winRate: 92 })], 'trending', now + 1000);
  const lines = readFileSync(snapshots, 'utf8').trim().split('\n').map(line => JSON.parse(line));
  assert.equal(lines.length, 2);
  assert.equal(lines[0].portfolioId, 'p-1');
  assert.equal(lines[0].winRatePct, 93);
  assert.equal(lines[1].winRatePct, 92);
});
