import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, readFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import {
  CANONICAL_INVO_HORIZONS,
  CANONICAL_INVO_SURFACES,
  InvoLeaderboardLedger,
  apiFilterForLeaderboardSurface,
  normalizeLeaderboardHorizon,
  normalizeLeaderboardSurface,
  parseLeaderboardItem,
} from '../src/invo-leaderboards.js';

const now = Date.parse('2026-09-16T00:00:00Z');

function portfolio(id: string, ownerId: string, percentChange: number) {
  return {
    id,
    title: `Portfolio ${id}`,
    ownerId,
    owner: { id: ownerId, username: `user-${ownerId}`, verified: true },
    percentChange,
    pnlUnit: '%',
    plSnapshot: { start: 1, end: 2 },
    changeInPl: percentChange / 10,
    avgPlRealized: percentChange / 20,
    openPositions: 2,
    closedPositions: 100,
    wonPositions: 80,
    lostPositions: 20,
    winRate: 80,
    liquidated: false,
    createdAt: '2025-01-01T00:00:00Z',
    updatedAt: '2026-09-15T23:59:00Z',
  };
}

test('canonical UI leaderboard surfaces include crown plus explicit horizons', () => {
  assert.deepEqual(CANONICAL_INVO_HORIZONS, ['1D', '1W', '1M', '1Y', 'AT']);
  assert.deepEqual(CANONICAL_INVO_SURFACES, ['CROWN', '1D', '1W', '1M', '1Y', 'AT']);
  assert.equal(normalizeLeaderboardHorizon('1d'), '1D');
  assert.equal(normalizeLeaderboardHorizon(' at '), 'AT');
  assert.equal(normalizeLeaderboardHorizon('trending'), null);
  assert.equal(normalizeLeaderboardSurface('trending'), 'CROWN');
  assert.equal(normalizeLeaderboardSurface('crown'), 'CROWN');
  assert.equal(apiFilterForLeaderboardSurface('CROWN'), 'trending');
  assert.equal(apiFilterForLeaderboardSurface('1W'), '1W');
});

test('leaderboard item persists exact surface, filter and rank provenance', () => {
  const crown = parseLeaderboardItem(portfolio('p0', 'o0', 99), 'CROWN', 1, now);
  assert.ok(crown);
  assert.equal(crown.surface, 'CROWN');
  assert.equal(crown.horizon, null);
  assert.equal(crown.sourceFilter, 'trending');

  const row = parseLeaderboardItem(portfolio('p1', 'o1', 123), '1W', 7, now);
  assert.ok(row);
  assert.equal(row.surface, '1W');
  assert.equal(row.horizon, '1W');
  assert.equal(row.sourceFilter, '1W');
  assert.equal(row.rank, 7);
  assert.equal(row.portfolioId, 'p1');
  assert.equal(row.percentChange, 123);
  assert.equal(row.closedPositions, 100);
  assert.equal(row.winRatePct, 80);
  assert.equal(row.sourceEndpoint, '/v1_0/trending/get_portfolios_pl');
});

test('crown and time horizons persist independently for the same portfolio', () => {
  const dir = mkdtempSync(join(tmpdir(), 'invo-leaderboards-'));
  const state = join(dir, 'leaderboards.json');
  const snapshots = join(dir, 'snapshots.jsonl');
  const ledger = new InvoLeaderboardLedger(state, snapshots);

  ledger.observePage([portfolio('p-shared', 'owner-1', 101), portfolio('p-crown', 'owner-4', 88)], 'CROWN', now, 0);
  ledger.observePage([portfolio('p-shared', 'owner-1', 12), portfolio('p-day', 'owner-2', 9)], '1D', now, 0);
  ledger.observePage([portfolio('p-week', 'owner-3', 40), portfolio('p-shared', 'owner-1', 33)], '1W', now, 0);
  ledger.observePage([portfolio('p-shared', 'owner-1', 300)], 'AT', now, 0);

  const report = ledger.report();
  assert.equal(report.latestBySurface.CROWN[0].portfolioId, 'p-shared');
  assert.equal(report.latestBySurface.CROWN[0].sourceFilter, 'trending');
  assert.equal(report.latestByHorizon['1D'][0].portfolioId, 'p-shared');
  assert.equal(report.latestByHorizon['1D'][0].percentChange, 12);
  assert.equal(report.latestByHorizon['1W'][1].portfolioId, 'p-shared');
  assert.equal(report.latestByHorizon['1W'][1].percentChange, 33);
  assert.equal(report.latestByHorizon.AT[0].percentChange, 300);

  const shared = report.crossSurface.find(row => row.portfolioId === 'p-shared');
  assert.ok(shared);
  assert.equal(shared.appearanceCount, 4);
  assert.deepEqual(shared.ranks, { CROWN: 1, '1D': 1, '1W': 2, AT: 1 });
  assert.deepEqual(shared.returns, { CROWN: 101, '1D': 12, '1W': 33, AT: 300 });

  const immutable = readFileSync(snapshots, 'utf8').trim().split('\n').map(line => JSON.parse(line));
  assert.equal(immutable.length, 7);
  assert.deepEqual(immutable.map(row => [row.surface, row.rank, row.portfolioId]), [
    ['CROWN', 1, 'p-shared'],
    ['CROWN', 2, 'p-crown'],
    ['1D', 1, 'p-shared'],
    ['1D', 2, 'p-day'],
    ['1W', 1, 'p-week'],
    ['1W', 2, 'p-shared'],
    ['AT', 1, 'p-shared'],
  ]);
});

test('pagination preserves global rank within each surface', () => {
  const dir = mkdtempSync(join(tmpdir(), 'invo-leaderboards-page-'));
  const ledger = new InvoLeaderboardLedger(join(dir, 'state.json'), join(dir, 'snapshots.jsonl'));
  ledger.observePage([portfolio('p51', 'o51', 1), portfolio('p52', 'o52', 2)], 'CROWN', now, 50);
  const crown = ledger.report().latestBySurface.CROWN;
  assert.deepEqual(crown.map(row => row.rank), [51, 52]);
});

test('a new observation replaces current surface view without deleting immutable history', () => {
  const dir = mkdtempSync(join(tmpdir(), 'invo-leaderboards-refresh-'));
  const state = join(dir, 'state.json');
  const snapshots = join(dir, 'snapshots.jsonl');
  const ledger = new InvoLeaderboardLedger(state, snapshots);
  ledger.observePage([portfolio('old', 'o1', 10)], 'CROWN', now, 0);
  ledger.observePage([portfolio('new', 'o2', 20)], 'CROWN', now + 60_000, 0);
  assert.deepEqual(ledger.report().latestBySurface.CROWN.map(row => row.portfolioId), ['new']);
  assert.equal(readFileSync(snapshots, 'utf8').trim().split('\n').length, 2);
});
