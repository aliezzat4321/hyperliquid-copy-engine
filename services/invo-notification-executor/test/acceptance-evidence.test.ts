import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { Lane3AcceptanceEvidence, PopulationReport } from '../src/acceptance-evidence.js';

function population(discovered: number, receiving: number): PopulationReport {
  return {
    policy: { minEvents: 20, minObservationDays: 7 },
    funnel: { discovered, receivingNotifications: receiving, shadowAssessable: 0, stale: 0, inactive: 0 },
    assessmentQueue: [],
    traders: discovered ? [{ eventCount: receiving, observationDays: ['2026-09-09'], symbols: ['BTC'], missingOrFailedReasons: {} }] : [],
  };
}

test('persists a frozen baseline and passes only on fresh growth with causal decisions', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-acceptance-'));
  const latest = join(dir, 'latest.json');
  const journal = join(dir, 'journal.jsonl');
  const audit = join(dir, 'audit.jsonl');
  writeFileSync(audit, '');
  const evidence = new Lane3AcceptanceEvidence(latest, journal, audit, 60_000);
  const first = evidence.record(population(1, 0), false, 1_000, true);
  assert.equal(first?.status, 'COLLECTING');

  writeFileSync(audit, JSON.stringify({ type: 'shadow_opened', signal: { key: 'one' } }) + '\n');
  const second = evidence.record(population(2, 1), false, 62_000);
  assert.equal(second?.windowStartedAt, new Date(1_000).toISOString());
  assert.equal(second?.growth.discovered, 1);
  assert.equal(second?.checks.everyCanonicalSignalAccountedFor, true);
  assert.equal(second?.status, 'RUNTIME_ACTIVITY_PROVEN');
  assert.equal(second?.promotionVerdict, 'NOT_EVALUATED');
  assert.equal(readFileSync(journal, 'utf8').trim().split('\n').length, 2);
});

test('fails closed for an unaccounted canonical signal and refuses live mode', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-acceptance-'));
  const audit = join(dir, 'audit.jsonl');
  writeFileSync(audit, JSON.stringify({ type: 'unexpected', signal: { key: 'one' } }) + '\n');
  const evidence = new Lane3AcceptanceEvidence(join(dir, 'latest.json'), join(dir, 'journal.jsonl'), audit, 1);
  assert.equal(evidence.record(population(1, 1), false, 1_000, true)?.status, 'COLLECTING');
  assert.throws(() => evidence.record(population(1, 1), true, 2_000, true), /shadow-only/);
});
