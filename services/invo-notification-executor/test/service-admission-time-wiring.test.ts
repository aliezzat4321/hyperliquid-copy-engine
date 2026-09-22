import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const serviceSource = readFileSync(resolve(process.cwd(), 'src/service.ts'), 'utf8');

test('prospective elite admission uses authoritative source trade time and separately passes processing time', () => {
  assert.match(
    serviceSource,
    /const eligibilityCutoffMs = signal\.sourceTimeMs;/,
    'NEW/ADD eligibility must be fixed at the source event boundary',
  );
  assert.doesNotMatch(serviceSource, /const eligibilityCutoffMs = decisionAtMs;/);
  assert.match(serviceSource, /eligibilityCutoffMs == null[\s\S]*eligibilityCutoffMs > decisionAtMs/);
  assert.match(serviceSource, /Math\.max\(10_000,[\s\S]*decisionAtMs,/);
});
