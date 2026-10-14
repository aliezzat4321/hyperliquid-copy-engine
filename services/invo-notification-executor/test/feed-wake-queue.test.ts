import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { FeedWakeQueue } from '../src/feed-wake-queue.js';

interface Wake {
  feedFilter: string;
  source: string;
}

test('a push wake cannot overwrite a pending fire_moves backfill', () => {
  const queue = new FeedWakeQueue<Wake>();

  queue.enqueue({ feedFilter: 'fire_moves', source: 'api_poll:fire_moves' });
  queue.enqueue({ feedFilter: 'following', source: 'push_notification' });

  assert.equal(queue.length, 2);
  assert.deepEqual(queue.shift(), {
    feedFilter: 'fire_moves',
    source: 'api_poll:fire_moves',
  });
  assert.deepEqual(queue.shift(), {
    feedFilter: 'following',
    source: 'push_notification',
  });
  assert.equal(queue.shift(), null);
});

test('repeated wakes coalesce within a surface without changing cross-surface order', () => {
  const queue = new FeedWakeQueue<Wake>();

  queue.enqueue({ feedFilter: 'fire_moves', source: 'api_poll:fire_moves' });
  queue.enqueue({ feedFilter: 'most_recent', source: 'api_poll:most_recent' });
  queue.enqueue({ feedFilter: 'fire_moves', source: 'push_notification' });

  assert.equal(queue.length, 2);
  assert.deepEqual(queue.shift(), {
    feedFilter: 'fire_moves',
    source: 'push_notification',
  });
  assert.deepEqual(queue.shift(), {
    feedFilter: 'most_recent',
    source: 'api_poll:most_recent',
  });
});

test('wakes arriving while one surface is blocked remain drainable without starvation', async () => {
  const queue = new FeedWakeQueue<Wake>();
  const processed: string[] = [];
  let releaseTrending!: () => void;
  const trendingBlocked = new Promise<void>(resolve => { releaseTrending = resolve; });

  const hydrate = async (wake: Wake) => {
    if (wake.feedFilter === 'trending') await trendingBlocked;
    processed.push(`${wake.source}:${wake.feedFilter}`);
  };

  const active = hydrate({ feedFilter: 'trending', source: 'api_poll:trending' });
  queue.enqueue({ feedFilter: 'fire_moves', source: 'api_poll:fire_moves' });
  queue.enqueue({ feedFilter: 'following', source: 'push_notification' });
  queue.enqueue({ feedFilter: 'most_recent', source: 'api_poll:most_recent' });

  releaseTrending();
  await active;
  for (let wake = queue.shift(); wake; wake = queue.shift()) await hydrate(wake);

  assert.deepEqual(processed, [
    'api_poll:trending:trending',
    'api_poll:fire_moves:fire_moves',
    'push_notification:following',
    'api_poll:most_recent:most_recent',
  ]);
});

test('service drains the per-surface queue instead of a replaceable pending slot', () => {
  const source = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  assert.match(source, /const pendingWakes = new FeedWakeQueue<FeedWake>\(\)/);
  assert.match(source, /pendingWakes\.enqueue\(\{ source, hints, receivedAtMs, feedFilter, heartbeat \}\)/);
  assert.match(source, /while \(\(current = pendingWakes\.shift\(\)\) != null\)/);
  assert.doesNotMatch(source, /let pendingWake:/);
});
