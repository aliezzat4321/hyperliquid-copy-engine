import test from 'node:test';
import assert from 'node:assert/strict';
import { publishThenFlushCapturedSignals } from '../src/scan-capture.js';
import type { InvoSignal } from '../src/notification-signal.js';

function signal(key: string, action: InvoSignal['action'] = 'open'): InvoSignal {
  return { key, postId: key, action, observedAtMs: 100, sourceTimeMs: 100,
    sourceTimeField: 'test', ownerId: 'owner', username: 'elite', portfolioId: 'p1',
    sourceBaseId: key, sourceBaseShortId: key, coin: 'BTC', side: 'long', leverage: 2,
    entryPrice: 100, closingPrice: action === 'close' ? 101 : null, entrySize: 1,
    resultingSourceSize: null };
}

test('healthy scan publishes admission then executes captured NEW exactly once before watermark', async () => {
  const seen = new Set<string>(); const order: string[] = []; let watermark = 0;
  const entry = signal('new-1');
  const result = await publishThenFlushCapturedSignals([
    { portfolioId: 'p1', signals: [entry], highWaterMs: 100,
      commit: value => { order.push('commit'); watermark = value; } },
  ], () => order.push('publish'), async current => {
    order.push(`execute:${current.key}`); seen.add(current.key);
  }, current => seen.has(current.key));
  assert.deepEqual(order, ['publish', 'execute:new-1', 'commit']);
  assert.equal(watermark, 100);
  assert.deepEqual(result, { committed: ['p1'], pending: [] });
  await publishThenFlushCapturedSignals([], () => order.push('republish'), async () => {
    throw new Error('no replay expected');
  }, current => seen.has(current.key));
  assert.equal(order.filter(value => value === 'execute:new-1').length, 1);
});

test('failed scan never publishes, executes, or commits captured NEW and next healthy scan replays it', async () => {
  const entry = signal('new-after-429'); let executions = 0; let commits = 0; let published = 0;
  const captured = [{ portfolioId: 'p1', signals: [entry], highWaterMs: 101,
    commit: () => { commits += 1; } }];
  // A 429/health failure exits before this flush function is called.
  assert.equal(executions, 0); assert.equal(commits, 0); assert.equal(published, 0);
  const seen = new Set<string>();
  await publishThenFlushCapturedSignals(captured, () => { published += 1; }, async current => {
    executions += 1; seen.add(current.key);
  }, current => seen.has(current.key));
  assert.equal(published, 1); assert.equal(executions, 1); assert.equal(commits, 1);
});

test('CLOSE executes promptly during suspension and is not part of delayed NEW flush', async () => {
  const entry = signal('new-buffered'); const close = signal('close-now', 'close');
  const order: string[] = []; const seen = new Set<string>();
  // CLOSED hydration follows its immediate execute path while OPEN capture is buffered.
  order.push(`execute:${close.key}`); seen.add(close.key);
  await publishThenFlushCapturedSignals([{ portfolioId: 'p1', signals: [entry], highWaterMs: 100,
    commit: () => order.push('commit') }], () => order.push('publish'), async current => {
    order.push(`execute:${current.key}`); seen.add(current.key);
  }, current => seen.has(current.key));
  assert.deepEqual(order, ['execute:close-now', 'publish', 'execute:new-buffered', 'commit']);
  assert.equal([...seen].filter(key => key === 'close-now').length, 1);
});

test('nonterminal signal retries without preventing a peer portfolio from committing', async () => {
  const retry = signal('retry-me');
  const peer = { ...signal('peer'), portfolioId: 'p2' };
  const seen = new Set<string>();
  const commits: string[] = [];
  let retryAttempts = 0;
  const batches = [
    { portfolioId: 'p1', signals: [retry], highWaterMs: 101, commit: () => commits.push('p1') },
    { portfolioId: 'p2', signals: [peer], highWaterMs: 102, commit: () => commits.push('p2') },
  ];
  const first = await publishThenFlushCapturedSignals(batches, () => {}, async current => {
    if (current.key === retry.key) { retryAttempts += 1; return; }
    seen.add(current.key);
  }, current => seen.has(current.key));
  assert.deepEqual(first, { committed: ['p2'], pending: ['p1'] });
  assert.deepEqual(commits, ['p2']);

  const second = await publishThenFlushCapturedSignals([batches[0]], () => {}, async current => {
    retryAttempts += 1; seen.add(current.key);
  }, current => seen.has(current.key));
  assert.deepEqual(second, { committed: ['p1'], pending: [] });
  assert.equal(retryAttempts, 2);
  assert.deepEqual(commits, ['p2', 'p1']);
});
