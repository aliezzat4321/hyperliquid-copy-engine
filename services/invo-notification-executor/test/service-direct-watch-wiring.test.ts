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
  assert.match(source, /ownedPortfolioIds\.has\(item\.target\.portfolioId\)/, 'closed-history polling must be limited to owned portfolios');
  assert.ok(pollGuard > scan, 'runtime scheduler must guard direct watch with !cfg.live');
});

test('service dedupes feed/direct races by source event and preserves admission/freshness gates', () => {
  const source = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  assert.match(source, /source-event:\$\{signal\.sourceBaseId\}:\$\{signal\.sourceTimeMs\}/);
  assert.match(source, /inFlightSourceEvents\.has\(signal\.sourceBaseId\)/);
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
  assert.match(source, /for \(const surface of cfg\.discoverySurfaces\)[\s\S]*startup_surface:\$\{surface\}/);
});
