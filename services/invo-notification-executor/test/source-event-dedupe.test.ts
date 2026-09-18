import assert from 'node:assert/strict';
import test from 'node:test';
import type { InvoSignal, SignalAction } from '../src/notification-signal.js';
import {
  closedLifecycleWasHandled,
  closeLifecycleKey,
  signalWasSeen,
  sourceEventKey,
} from '../src/source-event-dedupe.js';

function signal(action: SignalAction, time: number, postId: string, resultingSourceSize?: number): InvoSignal {
  return {
    key: `${postId}:${action}:base`, postId, action, observedAtMs: time + 1,
    sourceTimeMs: time, sourceTimeField: 'test', ownerId: 'owner', username: 'elite',
    portfolioId: 'portfolio', sourceBaseId: 'base', sourceBaseShortId: 'short',
    coin: 'BTC', side: 'long', leverage: 2, entryPrice: 1, closingPrice: action === 'close' ? 1 : null,
    entrySize: 1, resultingSourceSize,
  };
}

test('OPEN and CLOSE at the same timestamp are distinct handled lifecycle events', () => {
  const seen = new Set<string>();
  const open = signal('open', 100, 'feed-open');
  const close = signal('close', 100, 'feed-close');
  assert.equal(signalWasSeen(open, key => seen.has(key)), false);
  seen.add(sourceEventKey(open)!);
  assert.equal(signalWasSeen(close, key => seen.has(key)), false);
  seen.add(sourceEventKey(close)!);
  seen.add(closeLifecycleKey(close)!);
  assert.notEqual(sourceEventKey(open), sourceEventKey(close));
  assert.equal(seen.size, 3);
});

test('same OPEN from feed and direct at the same timestamp executes once', () => {
  const seen = new Set<string>();
  const feed = signal('open', 100, 'feed');
  const direct = signal('open', 100, 'direct');
  seen.add(sourceEventKey(feed)!);
  assert.equal(signalWasSeen(direct, key => seen.has(key)), true);
});

test('same CLOSE from feed and direct at different timestamps closes once', () => {
  const seen = new Set<string>();
  const feed = signal('close', 100, 'feed');
  const direct = signal('close', 101, 'direct');
  seen.add(closeLifecycleKey(feed)!);
  assert.equal(signalWasSeen(direct, key => seen.has(key)), true);
  assert.equal(closedLifecycleWasHandled(direct, key => seen.has(key)), true);
});

test('durable lifecycle close advances a differently timestamped direct hydration exactly once across restart', () => {
  const durableSeen = new Set<string>();
  const feed = signal('close', 100, 'feed-close');
  const direct = signal('close', 200, 'direct-close');
  durableSeen.add(sourceEventKey(feed)!);
  durableSeen.add(closeLifecycleKey(feed)!);

  let watermarkCommits = 0;
  let missedShortRoundtripEvidence = 1; // emitted by the first feed close only
  if (closedLifecycleWasHandled(direct, key => durableSeen.has(key))) watermarkCommits += 1;
  assert.equal(watermarkCommits, 1);
  assert.equal(missedShortRoundtripEvidence, 1);

  const restartedSeen = new Set(durableSeen);
  if (!closedLifecycleWasHandled(direct, key => restartedSeen.has(key))) {
    missedShortRoundtripEvidence += 1;
  }
  assert.equal(closedLifecycleWasHandled(direct, key => restartedSeen.has(key)), true);
  assert.equal(missedShortRoundtripEvidence, 1, 'restart must not re-log lifecycle evidence');
});

test('INCREASE identity includes action, time, and resulting source size', () => {
  const first = signal('increase', 100, 'feed', 3.5);
  const duplicate = signal('increase', 100, 'direct', 3.5);
  const nextSize = signal('increase', 100, 'direct-next', 4);
  assert.equal(sourceEventKey(first), sourceEventKey(duplicate));
  assert.notEqual(sourceEventKey(first), sourceEventKey(nextSize));
});

test('legacy OPEN keys prevent replay but do not suppress an ambiguous same-time CLOSE', () => {
  const seen = new Set(['source-event:base:100']);
  assert.equal(signalWasSeen(signal('open', 100, 'new-open'), key => seen.has(key)), true);
  assert.equal(signalWasSeen(signal('close', 100, 'new-close'), key => seen.has(key)), false);
});

test('legacy actionless OPEN key cannot suppress a same-time INCREASE', () => {
  const seen = new Set(['source-event:base:100']);
  assert.equal(signalWasSeen(signal('increase', 100, 'new-increase', 2), key => seen.has(key)), false);
});
