import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// A large legitimate feed backlog (or a future live topology with far more traders per
// cycle) must not be mistaken for a stalled loop. The service must beat real progress
// per backfill page and per processed signal instead of only once per wake() cycle, and
// every Hyperliquid HTTP request the feed path can make must be independently bounded.
test('feed loop beats watchdog progress per page and per signal, not only once per wake() cycle', () => {
  const source = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');

  const fetchAndProcess = source.indexOf('async function fetchAndProcess(');
  const wakeFn = source.indexOf('async function wake(');
  const pollLoop = source.indexOf('async function pollLoop()');
  assert.ok(fetchAndProcess >= 0 && wakeFn > fetchAndProcess && pollLoop > wakeFn);

  const fetchAndProcessBody = source.slice(fetchAndProcess, wakeFn);
  assert.match(fetchAndProcessBody, /fetchFeedBackfill\(\s*[\s\S]*?heartbeat,\s*\)/,
    'per-page progress must be threaded into fetchFeedBackfill as its onPage callback');
  assert.match(fetchAndProcessBody, /runSignalBatchBySource\(\s*[\s\S]*?heartbeat,?\s*\)/,
    'per-signal progress in the shadow batch path must be threaded through onProgress');
  const pendingCloseHeartbeats = fetchAndProcessBody.match(/heartbeat\(\)/g) ?? [];
  assert.ok(pendingCloseHeartbeats.length >= 3,
    'the pending-close, gap-recovery, baseline-recovery, and live sequential loops must all beat after each execute()');

  const wakeBody = source.slice(wakeFn, pollLoop);
  assert.match(wakeBody, /fetchAndProcess\([\s\S]*?heartbeat\)/,
    'wake() must forward its heartbeat into every fetchAndProcess call, including push-hydration retries');

  const pollLoopBody = source.slice(pollLoop, source.indexOf('\n}', pollLoop) + 2);
  assert.match(pollLoopBody, /await wake\([\s\S]*?=> loopWatchdog\.beat\('feed', Date\.now\(\)\)\)/,
    'pollLoop must give wake() a heartbeat that beats the real feed watchdog');
});

test('a single shadow signal execution beats the watchdog through its bounded funding catch-up wait', () => {
  const source = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  const executeUnlocked = source.indexOf('async function executeUnlocked(');
  const execute = source.indexOf('async function execute(', executeUnlocked);
  assert.ok(executeUnlocked >= 0 && execute > executeUnlocked);
  const body = source.slice(executeUnlocked, execute);
  assert.match(body, /heartbeat: \(\) => void = \(\) => \{\}/,
    'executeUnlocked must accept an optional heartbeat, defaulting to a no-op for non-feed callers');
  assert.match(body, /syncStagedFundingForClose\(\s*[\s\S]*?\{ onWait: heartbeat \}/,
    'the potentially long multi-funding-boundary catch-up wait must heartbeat on every poll');
});

test('every Hyperliquid info() request the feed path can make carries a bounded abort signal', () => {
  const source = readFileSync(new URL('../../src/hl-client.ts', import.meta.url), 'utf8');
  assert.match(source, /export const HL_HTTP_REQUEST_TIMEOUT_MS/);
  assert.match(source, /signal: AbortSignal\.timeout\(HL_HTTP_REQUEST_TIMEOUT_MS\)/,
    'the shared info() request must be timeout-bounded so no signal-processing step can hang unboundedly');
});

test('the feed watchdog limit formula no longer takes a page-count input', () => {
  const source = readFileSync(new URL('../../src/loop-watchdog.ts', import.meta.url), 'utf8');
  const fn = source.indexOf('export function feedWatchdogLimitMs');
  const end = source.indexOf('\n}', fn);
  const signature = source.slice(fn, end);
  assert.doesNotMatch(signature, /maxPages/,
    'the limit must not scale with the number of pages/signals in a cycle, only the bounded per-heartbeat unit of work');
  assert.match(signature, /signalProcessingBudgetMs/);
});
