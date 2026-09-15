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
    const result = await getFundingHistory('BTC', 0, 501);
    const rows = result.rows;
    assert.equal(calls, 2);
    assert.equal(rows.length, 502);
    assert.equal(Number(rows[0].time), 0);
    assert.equal(Number(rows.at(-1)?.time), 501);
    assert.deepEqual(result.diagnostics.returnedTimeMs.slice(-3), [500, 501, 501]);
    assert.equal(result.diagnostics.queryStartTimeMs, 0);
    assert.equal(result.diagnostics.queryEndTimeMs, 501);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('funding history preserves exact inclusive boundaries and rejects adjacent response rows', async () => {
  const originalFetch = globalThis.fetch;
  let requestBody: any;
  globalThis.fetch = (async (_url, init) => {
    requestBody = JSON.parse(String(init?.body));
    return jsonResponse([
      { time: 999, fundingRate: '9' },
      { time: 1000, fundingRate: '0.001', premium: '0.01' },
      { time: 2000, fundingRate: '-0.002', premium: '-0.02' },
      { time: 2001, fundingRate: '9' },
    ]);
  }) as typeof fetch;
  try {
    const result = await getFundingHistory('BTC', 1000, 2000);
    assert.equal(requestBody.startTime, 1000);
    assert.equal(requestBody.endTime, 2000);
    assert.deepEqual(result.rows.map(row => row.time), [1000, 2000]);
    assert.deepEqual(result.diagnostics.returnedRows, [
      { coin: undefined, fundingRate: '9', premium: undefined, time: 999 },
      { coin: undefined, fundingRate: '0.001', premium: '0.01', time: 1000 },
      { coin: undefined, fundingRate: '-0.002', premium: '-0.02', time: 2000 },
      { coin: undefined, fundingRate: '9', premium: undefined, time: 2001 },
    ]);
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
