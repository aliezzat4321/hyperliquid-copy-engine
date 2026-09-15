import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { NotificationState } from '../src/notification-state.js';

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

test('terminal sub-lot close reconciliation atomically clears exposure and consumes the signal', () => {
  const dir = mkdtempSync(join(tmpdir(), 'invo-notify-state-'));
  const path = join(dir, 'state.json');
  const state = new NotificationState(path);
  state.setManaged({
    coin: 'BTC', sourceBaseId: 'dust-btc', sourceBaseShortId: 'dust-short',
    sourcePostId: 'dust-post', side: 'long', openedAtMs: 1, size: 0.004,
    unresolvedAfterSourceClose: true, sourceCloseRetryAttempts: 4,
  });

  state.reconcileTerminalClose('dust-btc', 'dust-close');

  const restarted = new NotificationState(path);
  assert.equal(restarted.getManagedBySource('dust-btc'), null);
  assert.equal(restarted.hasSeen('dust-close'), true);
  assert.equal(restarted.managedCount(), 0);
});
