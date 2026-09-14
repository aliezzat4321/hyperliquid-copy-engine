import assert from 'node:assert/strict';
import test from 'node:test';
import { fetchFeedBackfill } from '../src/feed-backfill.js';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { NotificationState } from '../src/notification-state.js';
import { signalFromFeedPost } from '../src/notification-signal.js';

function pages(ids: string[][]) {
  let call = 0;
  return async (_lastPostId: string | null) => ({ items: (ids[call++] ?? []).map(id => ({ id })) });
}

test('restart backfill reaches a managed close beyond the former first-page window', async () => {
  const result = await fetchFeedBackfill(pages([
    ['new-5', 'new-4'],
    ['close-managed', 'new-2'],
    ['new-1', 'saved-high-water'],
  ]), 'saved-high-water', 5);

  assert.equal(result.cursorReached, true);
  assert.equal(result.pagesFetched, 3);
  assert.deepEqual(result.posts.map(post => post.id), ['new-5', 'new-4', 'close-managed', 'new-2', 'new-1']);

  // Replayed pages retain stable post ids, from which signal keys remain stable.
  const replay = await fetchFeedBackfill(pages([['new-5', 'new-4'], ['close-managed', 'new-2'], ['new-1', 'saved-high-water']]), 'saved-high-water', 5);
  assert.deepEqual(replay.posts.map(post => post.id), result.posts.map(post => post.id));
});

test('recovered managed close is applied exactly once when pages replay', async () => {
  const closePost = {
    id: 'close-managed',
    update: {
      id: 'update-close', baseId: 'base-managed', baseShortId: 'short-managed',
      ticker: 'SOL', verifiedTrade: true, directionLong: true, leverage: 5,
      entryPrice: 100, closingPrice: 90, isOpen: false,
      updatedAt: '2026-09-14T10:00:00Z',
      portfolio: { id: 'portfolio-1' }, owner: { id: 'owner-1', username: 'owner' },
    },
  };
  const first = await fetchFeedBackfill(pages([
    ['new-2', 'new-1'],
    ['close-managed', 'saved-high-water'],
  ]), 'saved-high-water', 3);
  first.posts[first.posts.findIndex(post => post.id === 'close-managed')] = closePost;

  const path = join(mkdtempSync(join(tmpdir(), 'invo-close-restart-')), 'state.json');
  const state = new NotificationState(path);
  state.setManaged({ coin: 'SOL', sourceBaseId: 'base-managed', sourceBaseShortId: 'short-managed', sourcePostId: 'open-post', side: 'long', openedAtMs: 1 });
  let closeExecutions = 0;
  for (const post of [...first.posts, ...first.posts]) {
    const signal = signalFromFeedPost(post);
    if (!signal || state.hasSeen(signal.key)) continue;
    if (signal.action === 'close' && state.getManagedBySource(signal.sourceBaseId)) {
      closeExecutions += 1;
      state.clearManagedBySource(signal.sourceBaseId);
    }
    state.markSeen(signal.key);
  }
  assert.equal(closeExecutions, 1);
  assert.equal(state.getManagedBySource('base-managed'), null);
});

test('bounded pagination reports an unreached saved cursor without advancing it', async () => {
  const result = await fetchFeedBackfill(pages([
    ['new-4', 'new-3'],
    ['new-2', 'new-1'],
  ]), 'missing-high-water', 2);

  assert.equal(result.cursorReached, false);
  assert.equal(result.pagesFetched, 2);
  assert.equal(result.savedCursor, 'missing-high-water');
  assert.equal(result.newestPostId, 'new-4');
});

test('initial bootstrap is bounded to the newest page', async () => {
  const result = await fetchFeedBackfill(pages([['new-2', 'new-1']]), null, 20);
  assert.equal(result.pagesFetched, 1);
  assert.equal(result.cursorReached, true);
  assert.equal(result.newestPostId, 'new-2');
});
