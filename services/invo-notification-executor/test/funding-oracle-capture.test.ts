import assert from 'node:assert/strict';
import test from 'node:test';
import { EventEmitter } from 'node:events';
import { execFile } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import {
  FundingOracleWorkerManager,
  clampFundingOracleMaxDelayMs,
  type FundingOracleCaptureResult,
} from '../src/funding-oracle-capture.js';
import { FundingBoundaryCapturer } from '../src/funding-oracle-worker.js';
import { FundingBoundaryStore } from '../src/funding-boundary-store.js';
import {
  applyFundingOracleResultToPosition,
  boundaryExposure,
  isPositionExposedAcrossBoundary,
  syncStagedFundingForClose,
} from '../src/funding-boundary-accounting.js';
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

test('production startup arms worker before Invo authentication delayed beyond 10s', { timeout: 15_000 }, async () => {
  const stagingPath = mkdtempSync(join(tmpdir(), 'funding-blocked-'));
  const harness = new URL('./fixtures/funding-worker-block-harness.js', import.meta.url);
  await promisify(execFile)(process.execPath, [harness.pathname, stagingPath], {
    timeout: 14_000,
    // Do not inherit the test runner's child-process protocol marker: this is an
    // application harness whose stdout is the asserted result, not a nested test file.
    env: { PATH: process.env.PATH ?? '' },
  });
  const output = readFileSync(join(stagingPath, 'harness-result.json'), 'utf8');
  const { result, fatal, health } = JSON.parse(output) as {
    result?: FundingOracleCaptureResult;
    fatal: string;
    health: ReturnType<FundingOracleWorkerManager['health']>;
  };
  assert.ok(result, 'worker must publish before main-thread message delivery');
  assert.equal(result.failureClass, undefined);
  assert.equal(result.oraclePrices?.BTC, 123.5);
  assert.ok(result.finalDelayMs <= 10_000);
  assert.equal(fatal, '');
  assert.equal(health.healthy, true);
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

function terminalResult(overrides: Partial<FundingOracleCaptureResult> = {}): FundingOracleCaptureResult {
  return {
    type: 'funding_oracle_result', fundingTimeMs: 1_000, attempts: [], retryCount: 0,
    finalObservedAtMs: 1_010, finalDelayMs: 10, oraclePrices: { BTC: 100 }, ...overrides,
  };
}

test('async result uses canonical positive boundary exposure eligibility', () => {
  const closedBefore = { ...position(), exposureCheckpoints: [{ atMs: 500, size: 1 }, { atMs: 999, size: 0 }] };
  const closedAt = { ...position(), exposureCheckpoints: [{ atMs: 500, size: 1 }, { atMs: 1_000, size: 0 }] };
  const partial = { ...position(), exposureCheckpoints: [{ atMs: 500, size: 2 }, { atMs: 900, size: 0.25 }] };
  const openedAfter = { ...position(), openedAtMs: 1_001, exposureCheckpoints: [{ atMs: 1_001, size: 1 }] };

  for (const excluded of [closedBefore, closedAt, openedAfter]) {
    const application = applyFundingOracleResultToPosition(excluded, terminalResult(), 100);
    assert.equal(application.applied, false);
    assert.equal(application.incomplete, false);
    assert.deepEqual(application.position.fundingOracleCheckpoints ?? [], []);
    assert.equal(application.position.fundingIncompleteReason, undefined);
  }
  assert.equal(boundaryExposure(partial, 1_000), 0.25);
  assert.equal(isPositionExposedAcrossBoundary(partial, 1_000), true);
  const applied = applyFundingOracleResultToPosition(partial, terminalResult(), 100);
  assert.equal(applied.applied, true);
  assert.deepEqual(applied.position.fundingOracleCheckpoints,
    [{ fundingTimeMs: 1_000, observedAtMs: 1_010, oraclePx: 100 }]);
});

test('async duplicate terminal result is idempotent', () => {
  const first = applyFundingOracleResultToPosition(position(), terminalResult(), 100);
  const duplicate = applyFundingOracleResultToPosition(first.position, terminalResult(), 100);
  assert.equal(duplicate.applied, false);
  assert.equal(duplicate.incomplete, false);
  assert.deepEqual(duplicate.position.fundingOracleCheckpoints, first.position.fundingOracleCheckpoints);
});

test('close waits for a staged terminal record and applies it', async () => {
  const store = new FundingBoundaryStore(mkdtempSync(join(tmpdir(), 'funding-wait-')));
  let now = 1_000;
  let sleeps = 0;
  const synced = await syncStagedFundingForClose(position(), 1_020, store, 100, {
    intervalMs: 1_000, now: () => now, pollMs: 10,
    sleep: async delay => {
      sleeps += 1;
      now += delay;
      store.publish(terminalResult());
    },
  });
  assert.equal(synced.waited, true);
  assert.equal(sleeps, 1);
  assert.deepEqual(synced.appliedBoundaries, [1_000]);
});

test('onWait fires on every poll so a caller-owned watchdog sees progress across multiple funding boundaries', async () => {
  // A position reopened after a long gap can legitimately cross many funding boundaries
  // in one close; each individually bounded by maxDelayMs but unbounded in count. onWait
  // must fire on every poll so a watchdog's silence window stays bounded by pollMs
  // instead of by the (open-ended) total multi-boundary catch-up wait.
  const store = new FundingBoundaryStore(mkdtempSync(join(tmpdir(), 'funding-onwait-')));
  let now = 1_000;
  let waits = 0;
  const pendingBoundaries = [1_000, 2_000, 3_000];
  const synced = await syncStagedFundingForClose(position(), 3_020, store, 100, {
    intervalMs: 1_000, now: () => now, pollMs: 10,
    sleep: async delay => {
      now += delay;
      const boundary = pendingBoundaries.shift();
      if (boundary != null) {
        store.publish(terminalResult({ fundingTimeMs: boundary, finalObservedAtMs: boundary + 10, finalDelayMs: 10 }));
      }
    },
    onWait: () => { waits += 1; },
  });
  assert.equal(synced.waited, true);
  assert.deepEqual(synced.appliedBoundaries, [1_000, 2_000, 3_000]);
  assert.equal(waits, 3, 'must heartbeat once per crossed funding boundary that had to wait');
});

test('close marks missing durable evidence incomplete after the deadline', async () => {
  const store = new FundingBoundaryStore(mkdtempSync(join(tmpdir(), 'funding-missing-')));
  const synced = await syncStagedFundingForClose(position(), 1_020, store, 100, {
    intervalMs: 1_000, now: () => 1_101,
  });
  assert.match(synced.position.fundingIncompleteReason ?? '', /missed durable oracle checkpoint/);
});

test('close rejects a late or failed staged terminal record', async () => {
  for (const [name, result] of [
    ['late', terminalResult({ finalObservedAtMs: 1_101, finalDelayMs: 101 })],
    ['failed', terminalResult({ oraclePrices: undefined, failureClass: 'deadline_exhausted' })],
  ] as const) {
    const store = new FundingBoundaryStore(mkdtempSync(join(tmpdir(), `funding-${name}-`)));
    store.publish(result);
    const synced = await syncStagedFundingForClose(position(), 1_020, store, 100,
      { intervalMs: 1_000, now: () => 1_020 });
    assert.match(synced.position.fundingIncompleteReason ?? '', /terminal oracle capture incomplete/);
  }
});

test('funding oracle causal delay is hard-clamped to ten seconds', () => {
  assert.equal(clampFundingOracleMaxDelayMs(60_000), 10_000);
  assert.equal(clampFundingOracleMaxDelayMs(9_000), 9_000);
  assert.equal(clampFundingOracleMaxDelayMs(1), 500);
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

test('nonzero worker exit is fatal and visible in health', () => {
  const worker = new FakeWorker();
  let fatal = '';
  const manager = new FundingOracleWorkerManager(
    { maxDelayMs: 100, stagingPath: '/unused', heartbeatMs: 1_000 }, () => {}, error => { fatal = error.message; },
    () => worker as any,
  );
  worker.emit('exit', 17);
  assert.equal(fatal, 'funding oracle worker exited with code 17');
  assert.equal(manager.health().healthy, false);
  assert.equal(manager.health().failure, 'funding oracle worker exited with code 17');
});

test('corrupt staged record fails loudly instead of producing complete economics', async () => {
  const path = mkdtempSync(join(tmpdir(), 'funding-corrupt-'));
  writeFileSync(join(path, '1000.json'), '{"version":1,"terminal":true,"result":');
  const store = new FundingBoundaryStore(path);
  assert.throws(() => store.read(1_000), /corrupt funding boundary record .*1000\.json/);
  await assert.rejects(
    syncStagedFundingForClose(position(), 1_020, store, 100, { intervalMs: 1_000, now: () => 1_020 }),
    /corrupt funding boundary record/,
  );
});

test('internally inconsistent staged success is corrupt and fails closed', () => {
  const path = mkdtempSync(join(tmpdir(), 'funding-corrupt-success-'));
  writeFileSync(join(path, '1000.json'), JSON.stringify({
    version: 1, terminal: true,
    result: terminalResult({ finalObservedAtMs: 1_010, finalDelayMs: 1 }),
  }));
  assert.throws(
    () => new FundingBoundaryStore(path).read(1_000),
    /corrupt funding boundary record .*inconsistent funding boundary result metadata/,
  );
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
  await new Promise(resolve => setTimeout(resolve, 1_000));
  assert.match(fatal, /missed .*heartbeat/);
});
