import assert from 'node:assert/strict';
import test from 'node:test';
import { getFundingHistory, getMeta, getL2Book, HL_HTTP_REQUEST_TIMEOUT_MS } from '../src/hl-client.js';

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

/**
 * A single hung Hyperliquid request has no ceiling unless the request itself carries a
 * bounded abort signal. Without this, no per-signal watchdog heartbeat budget can be a
 * sound bound: the request could hang forever, and "bound the request" is a prerequisite
 * for "beat progress at a bounded interval".
 */
test('HL_HTTP_REQUEST_TIMEOUT_MS is a positive, bounded default', () => {
  assert.ok(Number.isFinite(HL_HTTP_REQUEST_TIMEOUT_MS));
  assert.ok(HL_HTTP_REQUEST_TIMEOUT_MS > 0);
  assert.ok(HL_HTTP_REQUEST_TIMEOUT_MS <= 60_000);
});

test('getMeta attaches a live, bounded AbortSignal to the Hyperliquid info request', async () => {
  const originalFetch = globalThis.fetch;
  let capturedSignal: AbortSignal | undefined;
  globalThis.fetch = (async (_url, init) => {
    capturedSignal = init?.signal as AbortSignal | undefined;
    return jsonResponse({ universe: [] });
  }) as typeof fetch;
  try {
    await getMeta();
    assert.ok(capturedSignal instanceof AbortSignal);
    assert.equal(capturedSignal?.aborted, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

/**
 * getFundingHistory can page up to 20 sequential Hyperliquid requests inside a single
 * close signal's funding lookup (see fundingForPosition in shadow-market.ts). Before
 * onPage was threaded through, the feed watchdog only saw a heartbeat once the whole
 * signal finished, so a full 20-page pagination could silently run far past the
 * per-signal budget (4 sequential requests, see FEED_SIGNAL_MAX_SEQUENTIAL_REQUESTS in
 * service.ts) with no progress recorded in between. Every page must now beat onPage
 * immediately, keeping the watchdog's silence window bounded to one page regardless of
 * how many pages a boundary-crossing position needs.
 */
test('getFundingHistory beats onPage once per page across a full 20-page pagination window', async () => {
  const originalFetch = globalThis.fetch;
  const totalPages = 20;
  const rowsPerPage = 500;
  const startTime = 0;
  const endTime = 1_000_000;
  let fetchCalls = 0;
  globalThis.fetch = (async (_url, init) => {
    fetchCalls += 1;
    const body = JSON.parse(String(init?.body ?? '{}'));
    if (fetchCalls < totalPages) {
      const base = (fetchCalls - 1) * rowsPerPage;
      const batch = Array.from({ length: rowsPerPage }, (_, i) => ({
        coin: body.coin, fundingRate: '0.0001', premium: '0', time: base + i,
      }));
      return jsonResponse(batch);
    }
    // Final page returns a single row at/after endTime so pagination terminates
    // normally instead of hitting the maxPages exhaustion error.
    return jsonResponse([{ coin: body.coin, fundingRate: '0.0001', premium: '0', time: endTime }]);
  }) as typeof fetch;

  const heartbeatsAfterFetch: number[] = [];
  try {
    const query = await getFundingHistory('BTC', startTime, endTime, () => {
      heartbeatsAfterFetch.push(fetchCalls);
    });
    assert.equal(fetchCalls, totalPages, 'the full 20-page pagination window must be exercised');
    assert.equal(heartbeatsAfterFetch.length, totalPages, 'onPage must fire once per page, not once at the end');
    heartbeatsAfterFetch.forEach((countAtHeartbeat, index) => assert.equal(
      countAtHeartbeat, index + 1,
      'each heartbeat must land immediately after its own page request resolves',
    ));
    assert.ok(query.rows.length > 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('getL2Book request is abandoned once the bounded timeout elapses on a hung transport', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = ((_url, init) => new Promise((_resolve, reject) => {
    const signal = init?.signal as AbortSignal | undefined;
    signal?.addEventListener('abort', () => reject(new Error('aborted')));
  })) as typeof fetch;
  const keepAlive = setTimeout(() => {}, HL_HTTP_REQUEST_TIMEOUT_MS + 1_000);
  try {
    // Exercises the real configured HL_HTTP_REQUEST_TIMEOUT_MS end to end (no fake
    // timers): a hung transport must still be abandoned rather than hang the process.
    await assert.rejects(() => getL2Book('BTC'));
  } finally {
    clearTimeout(keepAlive);
    globalThis.fetch = originalFetch;
  }
});
