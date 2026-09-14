#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(sys.argv[1])
service = root / 'services/invo-notification-executor/src/service.ts'
helper = root / 'services/invo-notification-executor/src/gap-reconciliation.ts'
test = root / 'services/invo-notification-executor/test/gap-reconciliation.test.ts'

text = service.read_text()

old_import = "import { fetchFeedBackfill } from './feed-backfill.js';\n"
new_import = old_import + "import { planUnrecoverableGap } from './gap-reconciliation.js';\n"
if text.count(old_import) != 1:
    raise SystemExit('feed-backfill import anchor mismatch')
text = text.replace(old_import, new_import, 1)

old_gap = """  const posts = backfill.posts;
  if (saved && !backfill.cursorReached) {
    log({
      type: 'unrecoverable_feed_gap',
      feedFilter,
      savedCursor: saved,
      newestPostId: backfill.newestPostId,
      pagesFetched: backfill.pagesFetched,
      maxPages: cfg.feedMaxPages,
      exhausted: backfill.exhausted,
      managedCount: state.managedCount(),
      unresolvedManaged: state.snapshot().managed,
    });
    lastSuccessPollMs = Date.now();
    return 0;
  }
"""
new_gap = """  const posts = backfill.posts;
  if (saved && !backfill.cursorReached) {
    const gapPlan = planUnrecoverableGap(
      posts,
      post => signalFromFeedPost(post, receivedAtMs),
      sourceBaseId => Boolean(state.getManagedBySource(sourceBaseId)),
      key => state.hasSeen(key),
    );
    log({
      type: 'unrecoverable_feed_gap',
      feedFilter,
      savedCursor: saved,
      newestPostId: backfill.newestPostId,
      pagesFetched: backfill.pagesFetched,
      maxPages: cfg.feedMaxPages,
      exhausted: backfill.exhausted,
      managedCount: state.managedCount(),
      unresolvedManaged: state.snapshot().managed,
      cursorAdvanceAllowed: gapPlan.cursorAdvanceAllowed,
      recoverableOwnedCloses: gapPlan.ownedCloses.length,
    });
    // A missing historical cursor must never make a close already visible on a fetched
    // page disappear. Reconcile only closes for exposure this service still owns; leave
    // all other gap posts unseen and never advance the cursor across the missing range.
    for (const signal of gapPlan.ownedCloses) {
      await execute(signal, `${source}:gap_recovery`, receivedAtMs, feedFilter);
    }
    log({
      type: 'unrecoverable_feed_gap_reconciliation',
      feedFilter,
      savedCursor: saved,
      ownedCloseKeys: gapPlan.ownedCloses.map(signal => signal.key),
      reconciledOwnedCloses: gapPlan.ownedCloses.filter(signal => state.hasSeen(signal.key)).length,
      cursorAdvanceAllowed: false,
    });
    lastSuccessPollMs = Date.now();
    return gapPlan.ownedCloses.length;
  }
"""
if text.count(old_gap) != 1:
    raise SystemExit('unrecoverable gap anchor mismatch')
text = text.replace(old_gap, new_gap, 1)
service.write_text(text)

helper.write_text("""import type { InvoSignal } from './notification-signal.js';

export interface UnrecoverableGapPlan {
  ownedCloses: InvoSignal[];
  cursorAdvanceAllowed: false;
}

/**
 * Fail-closed plan for a fetched window that did not reach the durable feed cursor.
 * We may reconcile closes for exposure we already own because ignoring those closes
 * creates artificial exposure. We must not consume opens/increases/unowned closes and
 * must never advance the cursor across the missing historical range.
 */
export function planUnrecoverableGap(
  posts: any[],
  parseSignal: (post: any) => InvoSignal | null,
  hasManagedSource: (sourceBaseId: string) => boolean,
  hasSeen: (key: string) => boolean,
): UnrecoverableGapPlan {
  const ownedCloses: InvoSignal[] = [];
  const keys = new Set<string>();
  for (const post of posts) {
    const signal = parseSignal(post);
    if (!signal || signal.action !== 'close') continue;
    if (hasSeen(signal.key) || keys.has(signal.key)) continue;
    if (!hasManagedSource(signal.sourceBaseId)) continue;
    keys.add(signal.key);
    ownedCloses.push(signal);
  }
  ownedCloses.sort((a, b) => (a.sourceTimeMs ?? a.observedAtMs) - (b.sourceTimeMs ?? b.observedAtMs));
  return { ownedCloses, cursorAdvanceAllowed: false };
}
""")

test.write_text("""import assert from 'node:assert/strict';
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
""")
