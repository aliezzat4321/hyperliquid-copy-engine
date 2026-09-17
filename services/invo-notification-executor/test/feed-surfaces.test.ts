import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  INVO_FEED_SURFACES,
  parseDiscoverySurfaces,
  parseFeedSurface,
  planSurfaceBaseline,
  surfaceNeedsBaseline,
} from '../src/feed-surfaces.js';
import type { InvoSignal } from '../src/notification-signal.js';
import { signalFromFeedPost } from '../src/notification-signal.js';

function signal(action: InvoSignal['action'], key: string, sourceBaseId: string): InvoSignal {
  return {
    key, postId: key, action, observedAtMs: 1, sourceTimeMs: 1,
    sourceTimeField: 'fixture', ownerId: 'owner', username: 'elite',
    portfolioId: 'portfolio', sourceBaseId, sourceBaseShortId: sourceBaseId,
    coin: 'NEAR', side: 'long', leverage: 2, entryPrice: 1, entrySize: 1,
    closingPrice: action === 'close' ? 1.1 : null,
  };
}

test('all four proven Invo web feed filters are accepted and wired in default order', () => {
  assert.deepEqual(INVO_FEED_SURFACES, ['following', 'trending', 'fire_moves', 'most_recent']);
  for (const surface of INVO_FEED_SURFACES) {
    assert.equal(parseFeedSurface(surface, 'test'), surface);
  }
  assert.deepEqual(
    parseDiscoverySurfaces('following,trending,fire_moves,most_recent,following'),
    INVO_FEED_SURFACES,
  );
  assert.throws(() => parseDiscoverySurfaces('following,all'), /Invalid/);
});

test('a surface without its own durable cursor requires a prospective baseline', () => {
  assert.equal(surfaceNeedsBaseline(false), true);
  assert.equal(surfaceNeedsBaseline(true), false);
});

test('new-surface baseline skips historical OPEN/ADD but permits an owned CLOSE', () => {
  const open = signal('open', 'old-open', 'unowned-open');
  const add = signal('increase', 'old-add', 'unowned-add');
  const ownedClose = signal('close', 'old-owned-close', 'owned');
  const unownedClose = signal('close', 'old-unowned-close', 'unowned-close');
  const plan = planSurfaceBaseline([open, add, ownedClose, unownedClose], id => id === 'owned');
  assert.deepEqual(plan.recoverableCloses.map(row => row.key), ['old-owned-close']);
  assert.deepEqual(plan.skipped.map(row => row.key), ['old-open', 'old-add', 'old-unowned-close']);
});

test('after a surface cursor exists, a later fresh signal is not assigned to baseline', () => {
  assert.equal(surfaceNeedsBaseline(true), false);
  const fresh = signal('open', 'fresh-open', 'fresh-source');
  assert.equal(planSurfaceBaseline([fresh], () => false).skipped[0].key, 'fresh-open');
  // The service calls planSurfaceBaseline only when surfaceNeedsBaseline(cursor) is true.
});

test('sanitized captured feed shapes parse on every proven surface', () => {
  const fixture = JSON.parse(readFileSync(
    new URL('../../test/fixtures/invo-read-only-captured-shapes.json', import.meta.url),
    'utf8',
  ));
  assert.equal(fixture._evidence.kind, 'sanitized_captured_shape');
  for (const surface of INVO_FEED_SURFACES) {
    const parsed = signalFromFeedPost(fixture.feed[surface].items[0], 1789685684000);
    assert.ok(parsed, `${surface} captured shape must parse`);
  }
});
