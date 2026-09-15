import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { planUnrecoverableGap, reconcileUnrecoverableGap } from '../src/gap-reconciliation.js';
import type { InvoSignal } from '../src/notification-signal.js';
import { NotificationState } from '../src/notification-state.js';

function signal(
  key: string,
  sourceBaseId: string,
  action: InvoSignal['action'],
  sourceTimeMs: number | null,
): InvoSignal {
  return {
    key,
    postId: key.split(':')[0],
    action,
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
    closingPrice: action === 'close' ? 101 : null,
  };
}

test('unrecoverable gap reconciles owned stale/unknown closes only and forbids cursor advance', () => {
  const staleOwned = signal('p1:close:owned-stale', 'owned-stale', 'close', 1);
  const unknownTimeOwned = signal('p2:close:owned-unknown', 'owned-unknown', 'close', null);
  const unownedClose = signal('p3:close:unowned', 'unowned', 'close', 2);
  const ownedOpen = signal('p4:open:owned-stale', 'owned-stale', 'open', 3);
  const alreadySeen = signal('p5:close:owned-stale', 'owned-stale', 'close', 4);
  const byPost = new Map([
    ['p1', staleOwned],
    ['p2', unknownTimeOwned],
    ['p3', unownedClose],
    ['p4', ownedOpen],
    ['p5', alreadySeen],
  ]);

  const plan = planUnrecoverableGap(
    ['p1', 'p2', 'p3', 'p4', 'p5', 'p1'],
    post => byPost.get(post) ?? null,
    sourceBaseId => sourceBaseId.startsWith('owned-'),
    key => key === alreadySeen.key,
  );

  assert.equal(plan.cursorAdvanceAllowed, false);
  assert.deepEqual(plan.ownedCloses.map(row => row.key), [staleOwned.key, unknownTimeOwned.key]);
});

test('unrecoverable gap never promotes opens or unowned closes into reconciliation work', () => {
  const ownedOpen = signal('p1:open:owned', 'owned', 'open', 10);
  const unownedClose = signal('p2:close:other', 'other', 'close', 11);
  const plan = planUnrecoverableGap(
    ['p1', 'p2'],
    post => post === 'p1' ? ownedOpen : unownedClose,
    sourceBaseId => sourceBaseId === 'owned',
    () => false,
  );
  assert.equal(plan.cursorAdvanceAllowed, false);
  assert.deepEqual(plan.ownedCloses, []);
});

test('service gap orchestration executes visible owned close once and preserves durable cursor', async () => {
  const path = join(mkdtempSync(join(tmpdir(), 'invo-gap-service-')), 'state.json');
  const state = new NotificationState(path);
  state.setManaged({
    coin: 'BTC', sourceBaseId: 'owned', sourceBaseShortId: 'owned', sourcePostId: 'open',
    side: 'long', openedAtMs: 1, paper: true,
  });
  state.setFeedCursor('following', { postId: 'unreachable-cursor', observedAtMs: 10, source: 'prior_poll' });
  const ownedClose = signal('close-post:close:owned', 'owned', 'close', null);
  const unownedClose = signal('orphan-post:close:orphan', 'orphan', 'close', 20);
  const posts = ['close-post', 'orphan-post', 'close-post'];
  const byPost = new Map([['close-post', ownedClose], ['orphan-post', unownedClose]]);
  let executions = 0;

  const run = () => reconcileUnrecoverableGap(
    posts,
    post => byPost.get(post) ?? null,
    sourceBaseId => Boolean(state.getManagedBySource(sourceBaseId)),
    key => state.hasSeen(key),
    async close => {
      executions += 1;
      state.clearManagedBySource(close.sourceBaseId);
      state.markSeen(close.key);
    },
  );

  const first = await run();
  assert.deepEqual(first, {
    ownedCloseKeys: [ownedClose.key],
    reconciledOwnedCloses: 1,
    cursorAdvanceAllowed: false,
  });
  assert.equal(executions, 1);
  assert.equal(state.getManagedBySource('owned'), null);
  assert.equal(state.hasSeen(unownedClose.key), false);
  assert.equal(state.getFeedCursor('following')?.postId, 'unreachable-cursor');

  const restarted = new NotificationState(path);
  const replay = await reconcileUnrecoverableGap(
    posts,
    post => byPost.get(post) ?? null,
    sourceBaseId => Boolean(restarted.getManagedBySource(sourceBaseId)),
    key => restarted.hasSeen(key),
    async () => { executions += 1; },
  );
  assert.deepEqual(replay.ownedCloseKeys, []);
  assert.equal(executions, 1);
  assert.equal(restarted.getFeedCursor('following')?.postId, 'unreachable-cursor');
});
