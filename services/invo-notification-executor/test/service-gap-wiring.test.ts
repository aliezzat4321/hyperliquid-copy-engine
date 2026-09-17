import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { planUnrecoverableGap } from '../src/gap-reconciliation.js';
import type { InvoSignal } from '../src/notification-signal.js';
import { NotificationState } from '../src/notification-state.js';

function closeSignal(key: string, sourceBaseId: string, sourceTimeMs: number | null): InvoSignal {
  return {
    key,
    postId: key.split(':')[0],
    action: 'close',
    observedAtMs: 1_000_000,
    sourceTimeMs,
    sourceTimeField: sourceTimeMs == null ? null : 'update.closedAt',
    ownerId: 'owner',
    username: 'trader',
    portfolioId: 'portfolio',
    sourceBaseId,
    sourceBaseShortId: sourceBaseId,
    coin: 'BTC',
    side: 'long',
    leverage: 2,
    entryPrice: 100,
    entrySize: 1,
    closingPrice: 101,
  };
}

test('production service reconciles owned gap closes before any shadow zero-managed prospective rebase', () => {
  const serviceSource = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  const gapStart = serviceSource.indexOf('if (saved && !backfill.cursorReached)');
  const normalPath = serviceSource.indexOf('const tracked =', gapStart);
  assert.ok(gapStart >= 0 && normalPath > gapStart, 'unrecoverable-gap service branch must exist');

  const gapBranch = serviceSource.slice(gapStart, normalPath);
  const planIndex = gapBranch.indexOf('planUnrecoverableGap(');
  const executeLoopIndex = gapBranch.indexOf('for (const signal of gapPlan.ownedCloses)');
  const executeIndex = gapBranch.indexOf('await execute(signal', executeLoopIndex);
  const rebasePolicyIndex = gapBranch.indexOf('canProspectivelyRebaseGap(');
  const reconciliationLogIndex = gapBranch.indexOf("type: 'unrecoverable_feed_gap_reconciliation'");
  const guardedRebaseIndex = gapBranch.indexOf('if (prospectiveRebaseAllowed && backfill.newestPostId)');
  const setCursorIndex = gapBranch.indexOf('state.setFeedCursor(', guardedRebaseIndex);
  const initializedIndex = gapBranch.indexOf('initialized = true', setCursorIndex);
  const rebaseLogIndex = gapBranch.indexOf("type: 'unrecoverable_feed_gap_rebased'", initializedIndex);
  const returnIndex = gapBranch.indexOf('return gapPlan.ownedCloses.length', rebaseLogIndex);

  assert.ok(planIndex >= 0, 'service must build an owned-close-only gap plan');
  assert.ok(executeLoopIndex > planIndex, 'service must iterate planned owned closes');
  assert.ok(executeIndex > executeLoopIndex, 'service must await the real execute path for each owned close');
  assert.ok(rebasePolicyIndex > executeIndex, 'prospective rebase eligibility must be evaluated after owned-close reconciliation');
  assert.ok(reconciliationLogIndex > rebasePolicyIndex, 'service must record reconciliation and rebase eligibility');
  assert.ok(guardedRebaseIndex > reconciliationLogIndex, 'cursor advance must remain behind the explicit prospective rebase guard');
  assert.ok(setCursorIndex > guardedRebaseIndex, 'service may advance only inside the guarded rebase branch');
  assert.ok(initializedIndex > setCursorIndex, 'service must make the rebase the startup boundary before returning');
  assert.ok(rebaseLogIndex > initializedIndex, 'service must audit the prospective rebase');
  assert.ok(returnIndex > rebaseLogIndex, 'service must not return before the rebase is durably recorded');
});

test('stateful gap replay closes owned exposure once while preserving the durable high-water cursor', async () => {
  const statePath = join(mkdtempSync(join(tmpdir(), 'invo-service-gap-')), 'state.json');
  const state = new NotificationState(statePath);
  state.setManaged({
    coin: 'BTC', sourceBaseId: 'owned', sourceBaseShortId: 'owned', sourcePostId: 'open-post',
    side: 'long', openedAtMs: 1, paper: true, entryMid: 100, size: 1,
  });
  state.setFeedCursor('following', { postId: 'saved-high-water', observedAtMs: 10, source: 'test' });

  const ownedClose = closeSignal('close-post:close:owned', 'owned', null);
  const unownedClose = closeSignal('other-post:close:other', 'other', 5);
  const byPost = new Map<string, InvoSignal>([
    ['close-post', ownedClose],
    ['other-post', unownedClose],
  ]);

  const makePlan = () => planUnrecoverableGap(
    ['close-post', 'other-post'],
    post => byPost.get(post) ?? null,
    sourceBaseId => Boolean(state.getManagedBySource(sourceBaseId)),
    key => state.hasSeen(key),
  );

  const first = makePlan();
  assert.deepEqual(first.ownedCloses.map(signal => signal.key), [ownedClose.key]);
  for (const signal of first.ownedCloses) {
    state.clearManagedBySource(signal.sourceBaseId);
    state.markSeen(signal.key);
  }

  assert.equal(state.getManagedBySource('owned'), null);
  assert.equal(state.hasSeen(ownedClose.key), true);
  assert.equal(state.hasSeen(unownedClose.key), false);
  assert.equal(state.getFeedCursor('following')?.postId, 'saved-high-water');

  const replay = makePlan();
  assert.deepEqual(replay.ownedCloses, [], 'replay must not close the same exposure twice');
  assert.equal(state.getFeedCursor('following')?.postId, 'saved-high-water');
});
