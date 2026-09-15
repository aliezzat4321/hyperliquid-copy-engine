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

test('elite classification requires a proven portfolio-level sample', () => {
  const result = classifyPortfolio(portfolio(), now, 'trending');
  assert.ok(result);
  assert.equal(result.bucket, 'ELITE_CANDIDATE');
  assert.equal(result.portfolioId, 'p-elite');
  assert.equal(result.ownerId, 'owner-1');
  assert.equal(result.winRatePct, 93);
  assert.equal(result.closedPositions, 500);
});

test('two portfolios from the same owner never share eligibility', () => {
  const dir = mkdtempSync(join(tmpdir(), 'portfolio-ledger-'));
  const state = join(dir, 'state.json');
  const snapshots = join(dir, 'snapshots.jsonl');
  const ledger = new PortfolioCandidateLedger(state, snapshots);
  ledger.observe([
    portfolio({ id: 'p-good' }),
    portfolio({ id: 'p-bad', name: 'Bad Portfolio', closedPositions: 200, wonPositions: 70, lostPositions: 130, winRate: 35, percentChange: -60 }),
  ], 'trending', now);

  assert.equal(ledger.get('p-good')?.bucket, 'ELITE_CANDIDATE');
  assert.equal(ledger.get('p-bad')?.bucket, 'REJECTED_DEMOTED');
  assert.equal(ledger.report().uniqueOwnerCount, 1);
  assert.equal(ledger.report().uniquePortfolioCount, 2);
  assert.deepEqual(ledger.report().elitePortfolioIds, ['p-good']);
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
