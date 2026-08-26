import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { NotificationState } from '../src/notification-state.js';

test('persists dedupe and managed-position ownership across restart', () => {
  const dir = mkdtempSync(join(tmpdir(), 'invo-notify-state-'));
  const path = join(dir, 'state.json');
  const first = new NotificationState(path, 3);
  first.markSeen('a');
  first.setManaged({ coin: 'SOL', sourceBaseId: 'base-1', sourceBaseShortId: 'short-1', sourcePostId: 'post-1', side: 'long', openedAtMs: 1, localBaseShortId: 'local-1' });
  const second = new NotificationState(path, 3);
  assert.equal(second.hasSeen('a'), true);
  assert.equal(second.getManaged('sol')?.sourceBaseId, 'base-1');
  second.clearManaged('SOL');
  assert.equal(second.getManaged('SOL'), null);
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
