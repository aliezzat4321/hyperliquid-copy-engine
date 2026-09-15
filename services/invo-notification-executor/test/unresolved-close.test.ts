import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { NotificationState } from '../src/notification-state.js';
import { scheduleUnresolvedClose } from '../src/unresolved-close.js';

test('unresolved close intent survives restart and retry delay remains bounded', () => {
  const statePath = join(mkdtempSync(join(tmpdir(), 'unresolved-close-')), 'state.json');
  const state = new NotificationState(statePath);
  let pending = scheduleUnresolvedClose({ key: 'close:1', action: 'close' }, 'stale_book', 1_000, 500, 2_000);
  pending = scheduleUnresolvedClose(pending.signal, 'stale_book', 1_500, 500, 2_000, pending);
  pending = scheduleUnresolvedClose(pending.signal, 'stale_book', 2_500, 500, 2_000, pending);
  pending = scheduleUnresolvedClose(pending.signal, 'stale_book', 4_500, 500, 2_000, pending);
  assert.equal(pending.nextAttemptAtMs, 6_500, 'backoff must cap instead of abandoning retries');
  state.setManaged({
    coin: 'XRP', sourceBaseId: 'owned', sourceBaseShortId: 'owned', sourcePostId: 'open',
    side: 'long', openedAtMs: 1, paper: true, size: 1,
    unresolvedAfterSourceClose: true, unresolvedClose: pending,
  });
  const restarted = new NotificationState(statePath).getManagedBySource('owned');
  assert.equal(restarted?.unresolvedClose?.attempts, 4);
  assert.equal(restarted?.unresolvedClose?.signal.key, 'close:1');
});

test('production sweep retries seen close signals and excludes unresolved exposure from marks', () => {
  const source = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  assert.match(source, /await execute\(signal, 'unresolved_close_sweep', nowMs, cfg\.feedFilter, true\)/);
  assert.match(source, /!retryUnresolved && state\.hasSeen\(signal\.key\)/);
  assert.match(source, /markablePositions = paperPositions\.filter\(position => !position\.unresolvedAfterSourceClose\)/);
  assert.match(source, /mapBounded\(due, cfg\.unresolvedSweepConcurrency/);
});

test('collector and offline measurement use the same fail-closed book-age ceiling', () => {
  const service = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  const measurement = JSON.parse(readFileSync(new URL('../../../config/lane3_measurement.json', import.meta.url), 'utf8'));
  assert.match(service, /NOTIFICATION_TRADER_SHADOW_MAX_BOOK_AGE_MS', 750/);
  assert.equal(measurement.max_book_age_ms, 750);
});
