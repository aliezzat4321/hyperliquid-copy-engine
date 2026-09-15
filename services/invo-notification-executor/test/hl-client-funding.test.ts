import assert from 'node:assert/strict';
import test from 'node:test';
import { getFundingHistory } from '../src/hl-client.js';

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

test('funding history paginates and deduplicates until the requested end', async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls += 1;
    if (calls === 1) {
      return jsonResponse(Array.from({ length: 500 }, (_, time) => ({ time, fundingRate: '0.001' })));
    }
    return jsonResponse([
      { time: 500, fundingRate: '0.001' },
      { time: 501, fundingRate: '0.001' },
      { time: 501, fundingRate: '0.001' },
    ]);
  }) as typeof fetch;
  try {
    const rows = await getFundingHistory('BTC', 0, 501);
    assert.equal(calls, 2);
    assert.equal(rows.length, 502);
    assert.equal(Number(rows[0].time), 0);
    assert.equal(Number(rows.at(-1)?.time), 501);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('funding history fails closed instead of silently truncating at the page cap', async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    const page = calls;
    calls += 1;
    return jsonResponse(Array.from({ length: 500 }, (_, offset) => ({
      time: page * 500 + offset,
      fundingRate: '0.001',
    })));
  }) as typeof fetch;
  try {
    await assert.rejects(
      () => getFundingHistory('BTC', 0, 20_000),
      /Funding history pagination limit reached for BTC; economics incomplete/,
    );
    assert.equal(calls, 20);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
