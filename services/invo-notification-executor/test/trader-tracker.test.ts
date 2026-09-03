import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { signalFromFeedPost } from '../src/notification-signal.js';
import { TraderTracker } from '../src/trader-tracker.js';

const policy = { minEvents: 2, minObservationDays: 2, staleAfterMs: 100, inactiveAfterMs: 200 };
const post = (id: string, day: string, ownerId = 'owner-1') => ({
  id, createdAt: `${day}T00:00:00Z`, owner: { id: ownerId, username: '@Alice' },
  update: { id: `u-${id}`, baseId: `b-${id}`, ticker: 'btc', verifiedTrade: true,
    directionLong: true, leverage: 2, isOpen: true, portfolio: { id: 'portfolio-1' }, owner: { id: ownerId, username: 'Alice' } },
});

test('dedupes identities and events while preserving surface provenance', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'invo-traders-')), 'state.json');
  const tracker = new TraderTracker(path, policy);
  const item = post('p1', '2026-09-01');
  tracker.observe(item, 'following', signalFromFeedPost(item), 1_000);
  tracker.observe(item, 'trending', signalFromFeedPost(item), 1_001);
  const trader = tracker.report(1_001).traders[0];
  assert.equal(trader.id, 'invo-user:owner-1');
  assert.equal(trader.wallet, null);
  assert.equal(trader.eventCount, 1);
  assert.deepEqual(trader.sources, ['following', 'trending']);
});

test('promotes only on the predeclared event and day window, never PnL', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'invo-traders-')), 'state.json');
  const tracker = new TraderTracker(path, policy);
  const first = Date.parse('2026-09-01T01:00:00Z');
  const second = Date.parse('2026-09-02T01:00:00Z');
  tracker.observe(post('p1', '2026-09-01'), 'all', signalFromFeedPost(post('p1', '2026-09-01')), first);
  tracker.observe(post('p2', '2026-09-02'), 'all', signalFromFeedPost(post('p2', '2026-09-02')), second);
  const report = tracker.report(second + 50);
  assert.equal(report.funnel.discovered, 1);
  assert.equal(report.funnel.shadowAssessable, 1);
  assert.deepEqual(report.assessmentQueue, ['invo-user:owner-1']);
  assert.equal(report.traders[0].assessmentEligibleAtMs, second);
  assert.equal(Object.prototype.hasOwnProperty.call(report.traders[0], 'pnl'), false);
});

test('records missing reasons and automatically ages active to stale to inactive', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'invo-traders-')), 'state.json');
  const tracker = new TraderTracker(path, policy);
  const invalid = post('bad', '2026-09-01');
  invalid.update.verifiedTrade = false;
  tracker.observe(invalid, 'trending', null, 1_000);
  tracker.observe(invalid, 'trending', null, 1_100);
  assert.equal(tracker.report(1_050).traders[0].lifecycle, 'active');
  assert.equal(tracker.report(1_150).traders[0].lifecycle, 'stale');
  const retired = tracker.report(1_250);
  assert.equal(retired.traders[0].lifecycle, 'inactive');
  assert.equal(retired.funnel.activeTracked, 0);
  assert.equal(retired.traders[0].missingOrFailedReasons.not_verified_trade, 1);
});

test('persists population and evidence across restart', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'invo-traders-')), 'state.json');
  const item = post('p1', '2026-09-01');
  new TraderTracker(path, policy).observe(item, 'all', signalFromFeedPost(item), 1_000);
  const loaded = new TraderTracker(path, policy).report(1_001);
  assert.equal(loaded.funnel.discovered, 1);
  assert.equal(loaded.traders[0].eventCount, 1);
});

test('merges a portfolio-only discovery into the later canonical Invo user identity', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'invo-traders-')), 'state.json');
  const tracker = new TraderTracker(path, policy);
  const incomplete = post('p0', '2026-09-01');
  delete (incomplete.update.owner as any).id;
  delete (incomplete.owner as any).id;
  tracker.observe(incomplete, 'trending', null, 900);
  const complete = post('p1', '2026-09-02');
  tracker.observe(complete, 'all', signalFromFeedPost(complete), 1_000);
  const report = tracker.report(1_001);
  assert.equal(report.funnel.discovered, 1);
  assert.equal(report.traders[0].id, 'invo-user:owner-1');
  assert.deepEqual(report.traders[0].sources, ['all', 'trending']);
  assert.equal(report.traders[0].missingOrFailedReasons.missing_owner_id, 1);
});
