import assert from 'node:assert/strict';
import test from 'node:test';
import { getMeta, getL2Book, HL_HTTP_REQUEST_TIMEOUT_MS } from '../src/hl-client.js';

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
