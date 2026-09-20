import assert from 'node:assert/strict';
import test from 'node:test';
import { EventEmitter } from 'node:events';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  FundingOracleWorkerManager,
  startFundingOracleWorker,
  type FundingOracleCaptureResult,
} from '../src/funding-oracle-capture.js';
import { FundingBoundaryCapturer } from '../src/funding-oracle-worker.js';
import { FundingBoundaryStore } from '../src/funding-boundary-store.js';
import { syncStagedFundingForClose } from '../src/funding-boundary-accounting.js';
import type { ManagedPosition } from '../src/notification-state.js';

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

test('worker durably captures while the actual main thread is blocked beyond 10s', { timeout: 15_000 }, async t => {
  const stagingPath = mkdtempSync(join(tmpdir(), 'funding-blocked-'));
  const payload = encodeURIComponent(JSON.stringify([
    { universe: [{ name: 'BTC' }] }, [{ oraclePx: '123.5' }],
  ]));
  const intervalMs = 60_000;
  const boundary = Date.now() + 500;
  const manager = startFundingOracleWorker(
    { maxDelayMs: 10_000, retryBaseMs: 10, intervalMs, heartbeatMs: 500,
      initialBoundaryMs: boundary, endpoint: 'data:application/json,' + payload, stagingPath },
    () => {}, () => {},
  );
  t.after(async () => { await manager.worker.terminate(); });
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10_100);
  const result = new FundingBoundaryStore(stagingPath).read(boundary)?.result;
  assert.ok(result, 'worker must publish before main-thread message delivery');
  assert.equal(result.failureClass, undefined);
  assert.equal(result.oraclePrices?.BTC, 123.5);
  assert.ok(result.finalDelayMs <= 10_000);
});

function position(): ManagedPosition {
  return {
    coin: 'BTC', sourceBaseId: 'source', sourceBaseShortId: 'short', sourcePostId: 'post',
    side: 'long', openedAtMs: 500, size: 1, paper: true,
    exposureCheckpoints: [{ atMs: 500, size: 1 }], fundingOracleCheckpoints: [],
    fundingAccruedThroughMs: 500,
  };
}

test('close consumes timely durable evidence before worker message delivery', async () => {
  const store = new FundingBoundaryStore(mkdtempSync(join(tmpdir(), 'funding-race-')));
  store.publish({ type: 'funding_oracle_result', fundingTimeMs: 1_000, attempts: [], retryCount: 0,
    finalObservedAtMs: 1_010, finalDelayMs: 10, oraclePrices: { BTC: 100 } });
  const synced = await syncStagedFundingForClose(position(), 1_020, store, 100, { intervalMs: 1_000, now: () => 1_020 });
  assert.deepEqual(synced.position.fundingOracleCheckpoints,
    [{ fundingTimeMs: 1_000, observedAtMs: 1_010, oraclePx: 100 }]);
  assert.equal(synced.position.fundingIncompleteReason, undefined);
});

test('terminal boundary is immutable across duplicate writer and restart', () => {
  const path = mkdtempSync(join(tmpdir(), 'funding-immutable-'));
  const first = { type: 'funding_oracle_result' as const, fundingTimeMs: 1_000, attempts: [], retryCount: 0,
    finalObservedAtMs: 1_010, finalDelayMs: 10, oraclePrices: { BTC: 100 } };
  new FundingBoundaryStore(path).publish(first);
  new FundingBoundaryStore(path).publish({ ...first, oraclePrices: { BTC: 999 } });
  assert.equal(new FundingBoundaryStore(path).read(1_000)?.result.oraclePrices?.BTC, 100);
});

test('position eligibility excludes opens after and zero exposure before boundary', async () => {
  const store = new FundingBoundaryStore(mkdtempSync(join(tmpdir(), 'funding-eligibility-')));
  const openedAfter = { ...position(), openedAtMs: 1_001, exposureCheckpoints: [{ atMs: 1_001, size: 1 }] };
  const closedBefore = { ...position(), exposureCheckpoints: [{ atMs: 500, size: 1 }, { atMs: 900, size: 0 }] };
  assert.deepEqual((await syncStagedFundingForClose(openedAfter, 2_000, store, 100,
    { intervalMs: 1_000, now: () => 2_200 })).appliedBoundaries, []);
  assert.deepEqual((await syncStagedFundingForClose(closedBefore, 1_100, store, 100,
    { intervalMs: 1_000, now: () => 1_200 })).appliedBoundaries, []);
});

class FakeWorker extends EventEmitter {
  terminate(): Promise<number> { return Promise.resolve(0); }
}

test('clean worker exit code 0 is fatal', () => {
  const worker = new FakeWorker();
  let fatal = '';
  new FundingOracleWorkerManager(
    { maxDelayMs: 100, stagingPath: '/unused', heartbeatMs: 1_000 }, () => {}, error => { fatal = error.message; },
    () => worker as any,
  );
  worker.emit('exit', 0);
  assert.equal(fatal, 'funding oracle worker exited with code 0');
});

test('missed heartbeat is fatal', async () => {
  const worker = new FakeWorker();
  let now = 0;
  let fatal = '';
  new FundingOracleWorkerManager(
    { maxDelayMs: 100, stagingPath: '/unused', heartbeatMs: 5 }, () => {}, error => { fatal = error.message; },
    () => worker as any, () => now,
  );
  now = 16;
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.match(fatal, /missed heartbeat/);
});
