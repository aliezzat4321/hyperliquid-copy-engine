import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const serviceSource = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');

test('dry new exposure is gated by exact portfolio elite admission before open/reup execution', () => {
  const gate = serviceSource.indexOf("if (!cfg.live && signal.action !== 'close')");
  const admission = serviceSource.indexOf('eliteAdmissionFromState(', gate);
  const reup = serviceSource.indexOf("if (signal.action === 'increase' && existingManaged)", gate);
  const open = serviceSource.indexOf('await shadowOpen(', gate);
  assert.ok(gate >= 0, 'elite-only non-close gate missing');
  assert.ok(admission > gate, 'candidate admission call missing');
  assert.ok(reup > admission, 'increase path must come after admission gate');
  assert.ok(open > admission, 'open path must come after admission gate');
  assert.match(serviceSource, /reason: `shadow_\$\{candidateAdmission\.reason\}`/);
  assert.match(serviceSource, /shadowAdmissionMode: 'ELITE_ONLY'/);
  assert.match(serviceSource, /const eligibilityCutoffMs = signal\.sourceTimeMs;/,
    'authoritative source time must control prospective admission');
});

test('feed-originated signals are the primary admission path and cannot be blocked by direct-watch cooldown/capacity', () => {
  const gate = serviceSource.indexOf("if (!cfg.live && signal.action !== 'close')");
  const isDirectWatchSourced = serviceSource.indexOf('const isDirectWatchSourced = wakeSource.startsWith(', gate);
  const admission = serviceSource.indexOf('eliteAdmissionFromState(', gate);
  assert.ok(isDirectWatchSourced > gate, 'wakeSource-based direct-watch source detection missing');
  assert.ok(admission > isDirectWatchSourced, 'source classification must precede the admission call');
  assert.match(serviceSource, /isDirectWatchSourced \? cfg\.directWatchAdmissionIndexPath : undefined,/,
    'the direct-watch admission index must only be consulted for direct-watch-sourced signals');
  const call = serviceSource.slice(admission, serviceSource.indexOf(');', admission) + 2);
  assert.match(call, /isDirectWatchSourced,\s*\);$/,
    'requireDirectWatchAdmission must be wired to the wakeSource-derived classification');
  assert.match(serviceSource, /wakeSource\.startsWith\('elite_direct:'\)/,
    'only direct-watch\'s own emitted signals (elite_direct: prefix) require its admission proof');
});

test('owned closes bypass elite admission so demotion cannot orphan exposure', () => {
  assert.match(serviceSource, /if \(!cfg\.live && signal\.action !== 'close'\)/);
  const closePath = serviceSource.indexOf("if (signal.action === 'close')");
  const managedClose = serviceSource.indexOf('state.getManagedBySource(signal.sourceBaseId)', closePath);
  assert.ok(closePath >= 0 && managedClose > closePath);
});

test('source close rejection uses requested-residual dust classifier instead of reason alone', () => {
  assert.match(serviceSource, /shouldTerminallyDustReconcile\(/);
  assert.doesNotMatch(serviceSource, /const dustRejected = result\.reason === 'below_min_notional' \|\| result\.reason === 'lot_rounded_to_zero'/);
  assert.match(serviceSource, /economicsCompleteness: 'UNRESOLVED_EXPOSURE'/);
  assert.match(serviceSource, /sourceCloseNextRetryAtMs:/);
});


test('cross-surface canonical completion persists ingress key and feed completion uses canonical dedupe', () => {
  assert.match(serviceSource, /if \(alreadySeen\) \{[\s\S]*state\.markSeen\(signal\.key\);[\s\S]*return;[\s\S]*\}/);
  assert.match(serviceSource, /const allHandled = ordered\.every\(signal => signalWasSeen\(signal, key => state\.hasSeen\(key\)\)\);/);
});
