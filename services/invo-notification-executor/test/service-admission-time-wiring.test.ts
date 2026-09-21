import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const serviceSource = readFileSync(resolve(process.cwd(), 'src/service.ts'), 'utf8');

test('prospective elite admission uses processing decision time, not source trade time', () => {
  assert.match(
    serviceSource,
    /const eligibilityCutoffMs = decisionAtMs;/,
    'source trade time is provenance only and must not recreate candidate_state_from_future rejects',
  );
  assert.doesNotMatch(serviceSource, /const eligibilityCutoffMs = signal\.sourceTimeMs \?\? receivedAtMs;/);
});
