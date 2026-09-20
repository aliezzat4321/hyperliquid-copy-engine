import assert from 'node:assert/strict';
import test from 'node:test';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { NotificationState } from '../src/notification-state.js';

function statePath(prefix = 'invo-notify-state-') {
  return join(mkdtempSync(join(tmpdir(), prefix)), 'state.json');
}

test('missing state file initializes clean defaults without creating a file', () => {
  const path = statePath('notification-missing-');
  const state = new NotificationState(path);
  assert.equal(existsSync(path), false);
  assert.deepEqual(state.snapshot(), {
    version: 1, seen: [], managed: {}, feedCursors: {}, feedBaselines: {},
    observedOpenSourceIds: [], handledCloseSourceIds: [],
  });
});

test('valid legacy state migrates deterministically and preserves causal and exposure data', () => {
  const path = statePath('notification-legacy-full-');
  const legacy = {
    seen: ['dedupe-1', 'source-close:closed-1'],
    managed: {
      BTC: {
        coin: 'BTC', sourceBaseId: 'base-1', sourceBaseShortId: 'short-1', sourcePostId: 'post-1',
        username: 'trader', ownerId: 'owner-1', portfolioId: 'portfolio-1', side: 'long', openedAtMs: 10,
        size: 0.25, sourceSize: 0.5, unresolvedAfterSourceClose: true, fundingCarryUsd: 1.25,
        fundingAccruedThroughMs: 20, exposureCheckpoints: [{ atMs: 11, size: 0.25 }],
        fundingOracleCheckpoints: [{ fundingTimeMs: 12, observedAtMs: 13, oraclePx: 100 }],
      },
    },
    feedCursors: { following: { postId: 'cursor-post', observedAtMs: 30, source: 'poll' } },
    feedBaselines: { fire_moves: 31 },
    observedOpenSourceIds: ['base-1'],
    handledCloseSourceIds: ['closed-2'],
  };
  writeFileSync(path, JSON.stringify(legacy));
  const state = new NotificationState(path);
  assert.equal(state.snapshot().version, 1);
  assert.equal(state.hasSeen('dedupe-1'), true);
  assert.equal(state.hasObservedOpen('base-1'), true);
  assert.equal(state.hasHandledClose('closed-1'), true);
  assert.equal(state.hasHandledClose('closed-2'), true);
  assert.deepEqual(state.getFeedCursor('following'), legacy.feedCursors.following);
  assert.equal(state.hasFeedBaseline('following'), true);
  assert.equal(state.hasFeedBaseline('fire_moves'), true);
  const position = state.getManagedBySource('base-1');
  assert.equal(position?.portfolioId, 'portfolio-1');
  assert.equal(position?.fundingCarryUsd, 1.25);
  assert.deepEqual(position?.exposureCheckpoints, [{ atMs: 11, size: 0.25 }]);
  assert.deepEqual(position?.fundingOracleCheckpoints, [{ fundingTimeMs: 12, observedAtMs: 13, oraclePx: 100 }]);
  assert.equal(position?.pendingSourceClose?.action, 'close');
  state.markSeen('forces-versioned-write');
  assert.equal(JSON.parse(readFileSync(path, 'utf8')).version, 1);
});

test('corrupt JSON logs then throws without changing the file', () => {
  const path = statePath('notification-corrupt-');
  const corrupt = '{"seen": [broken';
  writeFileSync(path, corrupt);
  const logged: string[] = [];
  const originalError = console.error;
  console.error = (message?: unknown) => { logged.push(String(message)); };
  try {
    assert.throws(() => new NotificationState(path), SyntaxError);
  } finally {
    console.error = originalError;
  }
  assert.equal(JSON.parse(logged[0] ?? '{}').type, 'state_load_error');
  assert.equal(readFileSync(path, 'utf8'), corrupt);
});

test('wrong top-level shape fails closed', () => {
  const path = statePath('notification-shape-');
  writeFileSync(path, JSON.stringify([]));
  assert.throws(() => new NotificationState(path), /state must be an object/);
});

test('unknown state version fails closed', () => {
  const path = statePath('notification-version-');
  writeFileSync(path, JSON.stringify({ version: 999 }));
  assert.throws(() => new NotificationState(path), /unsupported notification state version/);
});

test('malformed managed and feed state fail closed instead of defaulting away', () => {
  const malformedManaged = statePath('notification-bad-managed-');
  writeFileSync(malformedManaged, JSON.stringify({ seen: [], managed: { BTC: { coin: 'BTC' } } }));
  assert.throws(() => new NotificationState(malformedManaged), /sourceBaseId/);

  const malformedCursor = statePath('notification-bad-cursor-');
  writeFileSync(malformedCursor, JSON.stringify({ seen: [], managed: {}, feedCursors: { following: { postId: 'p' } } }));
  assert.throws(() => new NotificationState(malformedCursor), /observedAtMs/);

  const malformedBaseline = statePath('notification-bad-baseline-');
  writeFileSync(malformedBaseline, JSON.stringify({ seen: [], managed: {}, feedBaselines: { following: 'now' } }));
  assert.throws(() => new NotificationState(malformedBaseline), /finite number/);
});

test('valid versioned restart preserves dedupe, lifecycle, cursors, baselines, and managed exposure', () => {
  const path = statePath('notification-versioned-restart-');
  writeFileSync(path, JSON.stringify({
    version: 1,
    seen: ['event-1'],
    managed: { 'base-1': { coin: 'ETH', sourceBaseId: 'base-1', sourceBaseShortId: 'short-1', sourcePostId: 'post-1', side: 'short', openedAtMs: 1, size: 2, fundingCarryUsd: -0.5 } },
    feedCursors: { recent: { postId: 'post-2', observedAtMs: 2, source: 'backfill' } },
    feedBaselines: { moves: 3 },
    observedOpenSourceIds: ['base-1'],
    handledCloseSourceIds: ['base-closed'],
  }));
  const restarted = new NotificationState(path);
  assert.equal(restarted.hasSeen('event-1'), true);
  assert.equal(restarted.hasObservedOpen('base-1'), true);
  assert.equal(restarted.hasHandledClose('base-closed'), true);
  assert.equal(restarted.getManagedBySource('base-1')?.fundingCarryUsd, -0.5);
  assert.deepEqual(restarted.getFeedCursor('recent'), { postId: 'post-2', observedAtMs: 2, source: 'backfill' });
  assert.equal(restarted.hasFeedBaseline('moves'), true);
});

test('persists dedupe and source-position ownership across restart', () => {
  const dir = mkdtempSync(join(tmpdir(), 'invo-notify-state-'));
  const path = join(dir, 'state.json');
  const first = new NotificationState(path, 3);
  first.markSeen('a');
  first.setFeedCursor('following', { postId: 'post-high-water', observedAtMs: 2, source: 'poll' });
  first.setManaged({ coin: 'SOL', sourceBaseId: 'base-1', sourceBaseShortId: 'short-1', sourcePostId: 'post-1', side: 'long', openedAtMs: 1, localBaseShortId: 'local-1' });
  const second = new NotificationState(path, 3);
  assert.equal(second.hasSeen('a'), true);
  assert.equal(second.getManagedBySource('base-1')?.coin, 'SOL');
  assert.deepEqual(second.getFeedCursor('following'), { postId: 'post-high-water', observedAtMs: 2, source: 'poll' });
  second.clearManagedBySource('base-1');
  assert.equal(second.getManagedBySource('base-1'), null);
});

test('an empty feed baseline survives restart without inventing a post cursor', () => {
  const dir = mkdtempSync(join(tmpdir(), 'invo-empty-baseline-'));
  const path = join(dir, 'state.json');
  const first = new NotificationState(path);
  first.markFeedBaselined('fire_moves', 10);
  assert.equal(first.getFeedCursor('fire_moves'), null);
  const restarted = new NotificationState(path);
  assert.equal(restarted.hasFeedBaseline('fire_moves'), true);
  assert.equal(restarted.getFeedCursor('fire_moves'), null);
});

test('keeps simultaneous same-coin positions independent by source base id', () => {
  const dir = mkdtempSync(join(tmpdir(), 'invo-notify-state-'));
  const path = join(dir, 'state.json');
  const state = new NotificationState(path);
  state.setManaged({ coin: 'BTC', sourceBaseId: 'carmine-btc', sourceBaseShortId: 'c1', sourcePostId: 'p1', username: 'carmine', side: 'long', openedAtMs: 1, size: 0.01 });
  state.setManaged({ coin: 'BTC', sourceBaseId: 'tyron-btc', sourceBaseShortId: 't1', sourcePostId: 'p2', username: 'tyron', side: 'short', openedAtMs: 2, size: 0.02 });

  assert.equal(state.managedCount(), 2);
  assert.equal(state.getManagedForCoin('btc').length, 2);
  assert.equal(state.getManagedBySource('carmine-btc')?.side, 'long');
  assert.equal(state.getManagedBySource('tyron-btc')?.side, 'short');

  state.clearManagedBySource('carmine-btc');
  assert.equal(state.getManagedBySource('carmine-btc'), null);
  assert.equal(state.getManagedBySource('tyron-btc')?.side, 'short');
});

test('bounds the persistent dedupe window', () => {
  const dir = mkdtempSync(join(tmpdir(), 'invo-notify-state-'));
  const path = join(dir, 'state.json');
  const state = new NotificationState(path, 2);
  state.markSeen('a'); state.markSeen('b'); state.markSeen('c');
  assert.equal(state.hasSeen('a'), false);
  assert.equal(state.hasSeen('b'), true);
  assert.equal(state.hasSeen('c'), true);
});

test('persists whether an opening lifecycle was observed', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'invo-open-observed-')), 'state.json');
  const first = new NotificationState(path);
  first.markObservedOpen('base-open');
  const restarted = new NotificationState(path);
  assert.equal(restarted.hasObservedOpen('base-open'), true);
  assert.equal(restarted.hasObservedOpen('base-never-open'), false);
});

test('restart synthesizes a retryable close signal for legacy unresolved source-close exposure', () => {
  const dir = mkdtempSync(join(tmpdir(), 'invo-notify-state-'));
  const path = join(dir, 'state.json');
  const first = new NotificationState(path);
  first.setManaged({
    coin: 'XRP',
    sourceBaseId: 'legacy-xrp',
    sourceBaseShortId: 'legacy-short',
    sourcePostId: 'legacy-post',
    username: 'legacy-user',
    side: 'long',
    openedAtMs: 1,
    size: 100,
    leverage: 3,
    unresolvedAfterSourceClose: true,
    sourceCloseLastReason: 'pre-repair-orphan',
  });

  const restarted = new NotificationState(path);
  const pending = restarted.getManagedBySource('legacy-xrp')?.pendingSourceClose;
  assert.ok(pending);
  assert.equal(pending.action, 'close');
  assert.equal(pending.coin, 'XRP');
  assert.equal(pending.sourceBaseId, 'legacy-xrp');
  assert.equal(pending.sourceTimeField, 'legacy_unresolved_source_close');
});

test('handled source close dominance persists across restart', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'notification-close-dominance-')), 'state.json');
  new NotificationState(path).markHandledClose('base-closed');
  assert.equal(new NotificationState(path).hasHandledClose('base-closed'), true);
});

test('legacy seen-only source-close migrates into durable close dominance', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'notification-legacy-close-')), 'state.json');
  writeFileSync(path, JSON.stringify({ seen: ['noise', 'source-close:legacy-base'], managed: {},
    feedCursors: {}, feedBaselines: {}, observedOpenSourceIds: [] }));
  const migrated = new NotificationState(path);
  assert.equal(migrated.hasHandledClose('legacy-base'), true);
  migrated.markSeen('persist-migration');
  assert.equal(new NotificationState(path).hasHandledClose('legacy-base'), true);
});

test('explicit pending close evidence is preserved instead of being overwritten by migration', () => {
  const dir = mkdtempSync(join(tmpdir(), 'invo-notify-state-'));
  const path = join(dir, 'state.json');
  const first = new NotificationState(path);
  first.setManaged({
    coin: 'ETH', sourceBaseId: 'base-eth', sourceBaseShortId: 'short-eth', sourcePostId: 'post-eth',
    side: 'short', openedAtMs: 1, size: 1, unresolvedAfterSourceClose: true,
    pendingSourceClose: {
      key: 'explicit-close', postId: 'explicit-post', action: 'close', observedAtMs: 2,
      sourceTimeMs: 2, sourceTimeField: 'update.closedAt', ownerId: 'owner', username: 'trader',
      portfolioId: 'portfolio', sourceBaseId: 'base-eth', sourceBaseShortId: 'short-eth', coin: 'ETH',
      side: 'short', leverage: 2, entryPrice: 100, closingPrice: 90, entrySize: 1,
    },
  });
  const restarted = new NotificationState(path);
  assert.equal(restarted.getManagedBySource('base-eth')?.pendingSourceClose?.key, 'explicit-close');
});
