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
  first.setManaged({ coin: 'SOL', sourceBaseId: 'base-1', sourceBaseShortId: 'short-1', sourcePostId: 'post-1', side: 'long', openedAtMs: 1, localBaseShortId: 'local-1' });
  const second = new NotificationState(path, 3);
  assert.equal(second.hasSeen('a'), true);
  assert.equal(second.getManagedBySource('base-1')?.coin, 'SOL');
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
