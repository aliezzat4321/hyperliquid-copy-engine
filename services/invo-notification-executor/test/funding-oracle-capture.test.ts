import assert from 'node:assert/strict';
import test from 'node:test';
import {
  startFundingOracleWorker,
  type FundingOracleCaptureResult,
} from '../src/funding-oracle-capture.js';
import { FundingBoundaryCapturer } from '../src/funding-oracle-worker.js';

function capturerFixture(request: (endpoint: string, timeoutMs: number) => Promise<Record<string, number>>) {
  let nowMs = 1_000;
  const capturer = new FundingBoundaryCapturer(
    { maxDelayMs: 10_000, retryBaseMs: 250, endpoint: 'unused' },
    () => nowMs,
    async delayMs => { nowMs += delayMs; },
    async (...args) => request(...args),
  );
  return { capturer, advance: (delayMs: number) => { nowMs += delayMs; } };
}

test('first oracle error retries and succeeds inside the strict window', async () => {
  let calls = 0;
  const fixture = capturerFixture(async () => {
    calls += 1;
    if (calls === 1) throw new TypeError('temporary network error');
    return { BTC: 100 };
  });
  const result = await fixture.capturer.capture(1_000);
  assert.equal(calls, 2);
  assert.equal(result.retryCount, 1);
  assert.equal(result.failureClass, undefined);
  assert.deepEqual(result.oraclePrices, { BTC: 100 });
  assert.deepEqual(result.attempts.map(attempt => attempt.outcome), ['error', 'success']);
  assert.ok(result.finalDelayMs <= 10_000);
});

test('retries that exhaust the deadline are explicitly incomplete', async () => {
  const fixture = capturerFixture(async () => { throw new TypeError('network unavailable'); });
  const result = await fixture.capturer.capture(1_000);
  assert.equal(result.failureClass, 'deadline_exhausted');
  assert.ok(result.attempts.length > 1);
  assert.equal(result.finalDelayMs, 10_000);
  assert.equal(result.oraclePrices, undefined);
});

test('duplicate callback shares one in-flight boundary request', async () => {
  let calls = 0;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const fixture = capturerFixture(async () => {
    calls += 1;
    await gate;
    return { ETH: 2_000 };
  });
  const first = fixture.capturer.capture(1_000);
  const duplicate = fixture.capturer.capture(1_000);
  assert.equal(first, duplicate);
  release();
  assert.deepEqual(await first, await duplicate);
  assert.equal(calls, 1);
});

test('restart after the boundary deadline fails closed without an oracle request', async () => {
  let calls = 0;
  const fixture = capturerFixture(async () => { calls += 1; return { BTC: 100 }; });
  fixture.advance(10_001);
  const result = await fixture.capturer.capture(1_000);
  assert.equal(result.failureClass, 'startup_after_deadline');
  assert.equal(result.attempts.length, 0);
  assert.equal(calls, 0);
});

test('independent worker captures while a simulated feed request remains blocked beyond 10s', async () => {
  // This models the feed promise being serialized for >10s; it deliberately never
  // resolves during the assertion, while capture runs in its own worker/event loop.
  const simulatedBlockedFeed = new Promise<void>(() => {});
  void simulatedBlockedFeed;
  const payload = encodeURIComponent(JSON.stringify([
    { universe: [{ name: 'BTC' }] }, [{ oraclePx: '123.5' }],
  ]));
  const result = await new Promise<FundingOracleCaptureResult>((resolve, reject) => {
    const worker = startFundingOracleWorker(
      { maxDelayMs: 500, retryBaseMs: 10, intervalMs: 1_000, endpoint: `data:application/json,${payload}` },
      value => { void worker.terminate(); resolve(value); },
      reject,
    );
  });
  assert.equal(result.failureClass, undefined);
  assert.equal(result.oraclePrices?.BTC, 123.5);
  assert.ok(result.finalDelayMs <= 500);
});
