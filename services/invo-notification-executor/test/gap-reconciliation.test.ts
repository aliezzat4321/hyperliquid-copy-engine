import assert from 'node:assert/strict';
import test from 'node:test';
import { planUnrecoverableGap } from '../src/gap-reconciliation.js';
import type { InvoSignal } from '../src/notification-signal.js';

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
