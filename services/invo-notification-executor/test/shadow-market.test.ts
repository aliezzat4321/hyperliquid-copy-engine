import assert from 'node:assert/strict';
import test from 'node:test';
import {
  fetchAssetBook,
  fundingForPosition,
  resetMetaCacheForTest,
} from '../src/shadow-market.js';
import type { ManagedPosition } from '../src/notification-state.js';

function response(value: unknown): Response {
  return new Response(JSON.stringify(value), { status: 200 });
}

test('book receipt timestamp excludes slow metadata and metadata is TTL cached', async () => {
  const originalFetch = globalThis.fetch;
  resetMetaCacheForTest();
  let metaCalls = 0;
  let bookCalls = 0;
  globalThis.fetch = (async (_url, init) => {
    const body = JSON.parse(String(init?.body));
    if (body.type === 'meta') {
      metaCalls += 1;
      await new Promise(resolve => setTimeout(resolve, 40));
      return response({ universe: [{ name: 'BTC', szDecimals: 3, maxLeverage: 50 }] });
    }
    bookCalls += 1;
    return response({ coin: 'BTC', time: Date.now(), levels: [[{ px: '100', sz: '1' }], [{ px: '101', sz: '1' }]] });
  }) as typeof fetch;
  try {
    const first = await fetchAssetBook('BTC');
    assert.ok(first);
    assert.ok(Date.now() - first.receivedAtMs >= 25, 'book receipt must be stamped before slow meta completes');
    await fetchAssetBook('BTC');
    assert.equal(metaCalls, 1);
    assert.equal(bookCalls, 2);
  } finally {
    globalThis.fetch = originalFetch;
    resetMetaCacheForTest();
  }
});

test('funding query starts exactly at the first captured boundary and records raw evidence', async () => {
  const originalFetch = globalThis.fetch;
  let request: any;
  globalThis.fetch = (async (_url, init) => {
    request = JSON.parse(String(init?.body));
    return response([{ coin: 'BTC', time: 3_600_000, fundingRate: '0.001', premium: '0' }]);
  }) as typeof fetch;
  const position: ManagedPosition = {
    coin: 'BTC', sourceBaseId: 'p', sourceBaseShortId: 'p', sourcePostId: 'open',
    side: 'long', openedAtMs: 1_234, paper: true, size: 2,
    exposureCheckpoints: [{ atMs: 1_234, size: 2 }],
    fundingOracleCheckpoints: [{ fundingTimeMs: 3_600_000, observedAtMs: 3_600_100, oraclePx: 100 }],
    fundingAccruedThroughMs: 1_234,
  };
  const evidence: any[] = [];
  try {
    const result = await fundingForPosition(position, 3_600_500, 1_000, row => evidence.push(row));
    assert.equal(request.startTime, 3_600_000);
    assert.equal(request.endTime, 3_600_500);
    assert.equal(result.fundingUsd, 0.2);
    assert.deepEqual(evidence[0].returnedRowTimesMs, [3_600_000]);
    assert.equal(evidence[0].rawRows[0].premium, '0');
  } finally {
    globalThis.fetch = originalFetch;
  }
});
