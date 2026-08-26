import assert from 'node:assert/strict';
import test from 'node:test';
import { extractNotificationHints, hintsMatchSignal, signalFromFeedPost } from '../src/notification-signal.js';

const basePost = {
  id: 'post-1',
  createdAt: '2026-08-26T18:00:00.000Z',
  owner: { id: 'owner-1', username: 'Bones' },
  update: {
    id: 'update-1', ticker: 'sol', verifiedTrade: true, directionLong: true,
    leverage: 5, entryPrice: 150, isOpen: true, baseId: 'base-1', baseShortId: 'short-1',
    portfolio: { id: 'portfolio-1' }, owner: { id: 'owner-1', username: '@Bones' },
  },
};

test('parses a verified open without a wallet identifier', () => {
  const signal = signalFromFeedPost(basePost, 123);
  assert.ok(signal);
  assert.equal(signal.action, 'open');
  assert.equal(signal.coin, 'SOL');
  assert.equal(signal.side, 'long');
  assert.equal(signal.username, 'bones');
  assert.equal(signal.portfolioId, 'portfolio-1');
  assert.equal(signal.sourceBaseId, 'base-1');
  assert.equal(signal.observedAtMs, 123);
});

test('parses close and increase actions', () => {
  const close = signalFromFeedPost({ ...basePost, id: 'post-close', update: { ...basePost.update, isOpen: false, closingPrice: 160 } });
  assert.equal(close?.action, 'close');
  const increase = signalFromFeedPost({ ...basePost, id: 'post-inc', update: { ...basePost.update, changes: { isAdded: false }, isOpen: true } });
  assert.equal(increase?.action, 'increase');
});

test('rejects unverified or ambiguous trade data', () => {
  assert.equal(signalFromFeedPost({ ...basePost, update: { ...basePost.update, verifiedTrade: false } }), null);
  assert.equal(signalFromFeedPost({ ...basePost, update: { ...basePost.update, directionLong: undefined } }), null);
  assert.equal(signalFromFeedPost({ ...basePost, update: { ...basePost.update, leverage: undefined } }), null);
});

test('notification contents are hints only and can match hydrated signal', () => {
  const hints = extractNotificationHints({ packageName: 'com.involio.app', title: '@Bones opened SOL', data: { portfolioId: 'portfolio-1', baseId: 'base-1' } });
  assert.deepEqual(hints, { username: 'bones', ticker: 'SOL', portfolioId: 'portfolio-1', baseId: 'base-1' });
  const signal = signalFromFeedPost(basePost)!;
  assert.equal(hintsMatchSignal(hints, signal), true);
  assert.equal(hintsMatchSignal({ ...hints, ticker: 'BTC' }, signal), false);
});
