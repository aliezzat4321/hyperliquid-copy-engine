import assert from 'node:assert/strict';
import test from 'node:test';
import type { InvoSignal, SignalAction } from '../src/notification-signal.js';
import { runSignalBatchBySource, SourceLifecycleQueue } from '../src/source-lifecycle.js';

function signal(action: SignalAction, base: string): InvoSignal {
  return {
    key: `${action}:${base}`, postId: action, action, observedAtMs: 1, sourceTimeMs: 1,
    sourceTimeField: 'test', ownerId: 'owner', username: 'elite', portfolioId: 'portfolio',
    sourceBaseId: base, sourceBaseShortId: base, coin: 'BTC', side: 'long', leverage: 2,
    entryPrice: 1, closingPrice: action === 'close' ? 1 : null, entrySize: 1,
  };
}

test('generic batch OPEN then CLOSE for one source cannot leave orphaned managed exposure', async () => {
  const queue = new SourceLifecycleQueue();
  const managed = new Set<string>();
  const events: string[] = [];
  await runSignalBatchBySource([signal('open', 'same'), signal('close', 'same')], async item => {
    await queue.run(item.sourceBaseId, async () => {
      if (item.action === 'open') managed.add(item.sourceBaseId);
      else managed.delete(item.sourceBaseId);
      events.push(item.action);
    });
  });
  assert.deepEqual(events, ['open', 'close']);
  assert.equal(managed.has('same'), false);
});

test('feed and direct ingress serialize the same source while different sources overlap', async () => {
  const queue = new SourceLifecycleQueue();
  const events: string[] = [];
  let releaseSame!: () => void;
  const gate = new Promise<void>(resolve => { releaseSame = resolve; });
  const feed = queue.run('same', async () => { events.push('same-open-start'); await gate; events.push('same-open-end'); });
  const direct = queue.run('same', async () => { events.push('same-close'); });
  const other = queue.run('other', async () => { events.push('other-open'); });
  await other;
  assert.deepEqual(events, ['same-open-start', 'other-open']);
  releaseSame();
  await Promise.all([feed, direct]);
  assert.deepEqual(events, ['same-open-start', 'other-open', 'same-open-end', 'same-close']);
});
