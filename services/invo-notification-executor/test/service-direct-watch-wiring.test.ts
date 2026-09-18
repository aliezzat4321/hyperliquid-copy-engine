import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

test('service wires elite direct watch only in shadow and through normal execute admission', () => {
  const source = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  const hydrate = source.indexOf('async function hydrateDirectTarget');
  const execute = source.indexOf('await execute(signal, `elite_direct:${reason}`', hydrate);
  const scan = source.indexOf('async function scanEliteDirectWatch');
  const liveGuard = source.indexOf('if (cfg.live || nowMs < directWatchBackoffUntilMs) return', scan);
  const bounded = source.indexOf('cfg.directWatchMaxHydratesPerScan', scan);
  const pollGuard = source.indexOf('if (!cfg.live && directNowMs - lastDirectWatchScanMs >= cfg.directWatchScanMs)');
  assert.ok(hydrate >= 0 && scan > hydrate);
  assert.ok(execute > hydrate && execute < scan, 'direct signals must flow through execute()');
  assert.ok(liveGuard > scan, 'direct watcher must fail closed in live mode');
  assert.ok(bounded > liveGuard, 'per-scan hydration work must be bounded');
  assert.match(source, /planDirectHydrations\([\s\S]*cfg\.directWatchFallbackPollMs,[\s\S]*cfg\.directWatchMaxHydratesPerScan/, 'deadline-first planner must bound periodic direct polling');
  assert.match(source, /planClosedHydrations\([\s\S]*cfg\.directWatchClosedPollMs,[\s\S]*cfg\.directWatchMaxClosedHydratesPerScan/, 'closed history must be scheduled for every selected target with a separate bound');
  assert.match(source, /closed_history_baseline[\s\S]*replayedSignals: 0/, 'first closed-history observation must only establish a baseline');
  assert.match(source, /unownedCloseEvidence\(signal, observedOpen\)/, 'closed-only lifecycle evidence must use the explicit classifier');
  assert.ok(pollGuard > scan, 'runtime scheduler must guard direct watch with !cfg.live');
});

test('service routes feed/direct races through action-aware dedupe and preserves admission/freshness gates', () => {
  const source = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  assert.match(source, /signalWasSeen\(signal/);
  assert.match(source, /sourceEventKey\(signal\)/);
  assert.match(source, /closeLifecycleKey\(signal\)/, 'feed/direct close timestamps may differ but one lifecycle closes once');
  assert.match(source, /inFlightSourceEvents\.has\(inFlightKey\)/);
  assert.match(source, /ageMs > cfg\.maxSignalAgeMs/);
  assert.match(source, /shadow_\$\{candidateAdmission\.reason\}/);
  assert.match(source, /cfg\.candidateSnapshotsPath/, 'admission must use immutable pre-trade candidate snapshots');
});

test('service baselines independently from each surface durable cursor', () => {
  const source = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  assert.match(source, /const saved = state\.getFeedCursor\(feedFilter\)/);
  assert.match(source, /if \(surfaceNeedsBaseline\(state\.hasFeedBaseline\(feedFilter\)\)\)/);
  assert.doesNotMatch(source, /let initialized = false/);
  assert.match(source, /state\.setFeedCursor\(feedFilter, \{ postId: backfill\.newestPostId/);
  const baselineBranch = source.slice(source.indexOf('if (surfaceNeedsBaseline'));
  assert.ok(
    baselineBranch.indexOf('state.markFeedBaselined(feedFilter, baselineAtMs)')
      < baselineBranch.indexOf("await execute(signal, 'surface_baseline_owned_close_recovery'"),
    'the durable surface boundary must precede retryable owned-close execution',
  );
  assert.match(baselineBranch, /startupHandled[\s\S]*state\.setFeedCursor/, 'cursor may remain gated by owned-close recovery');
  assert.match(source, /for \(const surface of cfg\.discoverySurfaces\)[\s\S]*startup_surface:\$\{surface\}/);
});

test('closed hydration fails closed on explicit ordering violations', () => {
  const source = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  assert.match(source, /validateClosedPageOrdering/);
  assert.match(source, /type: 'ordering_violation'/);
  assert.match(source, /overflowRiskCounted: true, watermarkCommitted: false/);
});
