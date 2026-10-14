import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  FeedSurfaceTimeoutRecovery,
  isInvoTransportTimeout,
} from '../src/feed-surface-timeout-recovery.js';

test('a timeout cools down only its surface and backs off repeated failures', () => {
  const recovery = new FeedSurfaceTimeoutRecovery(30_000, 120_000);

  assert.deepEqual(recovery.noteTimeout('trending', 1_000), {
    consecutiveTimeouts: 1,
    cooldownUntilMs: 31_000,
  });
  assert.equal(recovery.rotatingPollCooldown('following', 2_000), null);
  assert.equal(recovery.rotatingPollCooldown('fire_moves', 2_000), null);
  assert.equal(recovery.rotatingPollCooldown('most_recent', 2_000), null);
  assert.equal(recovery.rotatingPollCooldown('trending', 30_999)?.consecutiveTimeouts, 1);
  assert.equal(recovery.rotatingPollCooldown('trending', 31_000), null);

  assert.deepEqual(recovery.noteTimeout('trending', 31_000), {
    consecutiveTimeouts: 2,
    cooldownUntilMs: 91_000,
  });
});

test('a successful retry clears timeout history', () => {
  const recovery = new FeedSurfaceTimeoutRecovery(10, 40);
  recovery.noteTimeout('trending', 0);
  recovery.noteTimeout('trending', 10);
  recovery.noteSuccess('trending');

  assert.equal(recovery.rotatingPollCooldown('trending', 11), null);
  assert.equal(recovery.noteTimeout('trending', 20).consecutiveTimeouts, 1);
});

test('recognizes AbortSignal timeout errors without classifying ordinary failures', () => {
  assert.equal(isInvoTransportTimeout(Object.assign(new Error('aborted'), { name: 'AbortError' })), true);
  assert.equal(isInvoTransportTimeout(Object.assign(new Error('timed out'), { name: 'TimeoutError' })), true);
  assert.equal(isInvoTransportTimeout(new Error('socket reset')), false);
});

test('service skips only rotating polls while push hydration still enters the wake queue', () => {
  const source = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  const pollLoop = source.slice(source.indexOf('async function pollLoop()'), source.indexOf('async function directWatchLoop()'));
  const ingress = source.slice(source.indexOf("req.url === '/invo-notification'"), source.indexOf("return json(res, 404"));

  assert.match(pollLoop, /rotatingPollCooldown\(surface, Date\.now\(\)\)/);
  assert.match(pollLoop, /cursorAdvanced: false/);
  assert.match(pollLoop, /continue;/);
  assert.match(ingress, /void wake\('push_notification'/);
  assert.doesNotMatch(ingress, /rotatingPollCooldown/);
});
