import assert from 'node:assert/strict';
import test from 'node:test';
import type { ManagedPosition } from '../src/notification-state.js';
import { fetchAssetBook, fundingForPosition } from '../src/shadow-market.js';

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

test('asset book receipt timestamp is captured at l2 completion before metadata completion', async () => {
  const originalFetch = globalThis.fetch;
  let metaCompletedAtMs = 0;
  globalThis.fetch = (async (_url, init) => {
    const body = JSON.parse(String(init?.body ?? '{}'));
    if (body.type === 'l2Book') {
      return jsonResponse({
        coin: 'BTC',
        time: Date.now() - 100,
        levels: [
          [{ px: '100', sz: '2' }],
          [{ px: '101', sz: '2' }],
        ],
      });
    }
    if (body.type === 'meta') {
      await new Promise(resolve => setTimeout(resolve, 25));
      metaCompletedAtMs = Date.now();
      return jsonResponse({ universe: [{ name: 'BTC', szDecimals: 3, maxLeverage: 50 }] });
    }
    throw new Error(`unexpected info request: ${body.type}`);
  }) as typeof fetch;

  try {
    const assetBook = await fetchAssetBook('BTC');
    assert.ok(assetBook);
    assert.ok(metaCompletedAtMs > 0);
    assert.ok(assetBook.receivedAtMs < metaCompletedAtMs, 'book receipt must not wait for metadata');
    assert.equal(assetBook.book?.receivedAtMs, assetBook.receivedAtMs);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('funding failures expose exact query boundaries and raw-enough returned rows while failing closed', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (_url, init) => {
    const body = JSON.parse(String(init?.body ?? '{}'));
    assert.equal(body.type, 'fundingHistory');
    return jsonResponse([]);
  }) as typeof fetch;

  const position: ManagedPosition = {
    coin: 'BTC',
    sourceBaseId: 'source-1',
    sourceBaseShortId: 'short-1',
    sourcePostId: 'post-1',
    side: 'long',
    openedAtMs: 1_000,
    paper: true,
    size: 1,
    exposureCheckpoints: [{ atMs: 1_000, size: 1 }],
    fundingOracleCheckpoints: [{ fundingTimeMs: 2_000, observedAtMs: 2_050, oraclePx: 100 }],
    fundingCarryUsd: 0,
    fundingAccruedThroughMs: 1_000,
  };

  try {
    await assert.rejects(
      () => fundingForPosition(position, 2_500, 500),
      (err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        assert.match(message, /Missing funding-history row for captured oracle interval 2000/);
        assert.match(message, /"queryStartTimeMs":1000/);
        assert.match(message, /"queryEndTimeMs":2500/);
        assert.match(message, /"returnedTimeMs":\[\]/);
        assert.match(message, /"returnedRows":\[\]/);
        return true;
      },
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
