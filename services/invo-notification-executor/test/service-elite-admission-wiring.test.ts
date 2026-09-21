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
  assert.match(serviceSource, /const eligibilityCutoffMs = decisionAtMs;/,
    'receipt/processing time, never source trade time, must control prospective admission');
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
