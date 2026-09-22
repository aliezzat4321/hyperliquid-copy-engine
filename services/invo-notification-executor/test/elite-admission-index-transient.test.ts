import assert from 'node:assert/strict';
import { mkdtempSync, truncateSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { eliteAdmissionFromState, shouldPersistAdmissionDenial } from '../src/elite-admission.js';
import { ELITE_SELECTOR_VERSION } from '../src/portfolio-candidates.js';

const now = Date.UTC(2026, 8, 22, 9, 45, 0);
const portfolioId = 'elite-transient-index';

function setup() {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-transient-index-'));
  const statePath = join(dir, 'portfolio-candidates.json');
  const snapshotsPath = join(dir, 'portfolio-candidate-snapshots.jsonl');
  const admissionPath = join(dir, 'elite-direct-watch-admissions.json');
  const observedAtMs = now - 1_000;
  writeFileSync(statePath, JSON.stringify({
    version: 1,
    selectorVersion: ELITE_SELECTOR_VERSION,
    lastObservedAtMs: observedAtMs,
    firstEliteAtMs: { [portfolioId]: observedAtMs },
    portfolios: {
      [portfolioId]: {
        portfolioId,
        selectorVersion: ELITE_SELECTOR_VERSION,
        observedAtMs,
        bucket: 'ELITE_CANDIDATE',
        closedPositions: 25,
        closedPositionsPerDay: 2,
        winRatePct: 80,
        percentChange: 200,
        winLossRatio: 4,
        daysActive: 10,
        recentActivityDaysAgo: 0,
        liquidated: false,
        sourceFilter: 'test',
        score: 90,
      },
    },
  }));
  writeFileSync(admissionPath, JSON.stringify({
    version: 1,
    generatedAtMs: now,
    healthy: true,
    suspensionReason: null,
    rows: {
      [portfolioId]: {
        portfolioId,
        selectorVersion: ELITE_SELECTOR_VERSION,
        admittedAtMs: observedAtMs,
      },
    },
  }));
  return { statePath, snapshotsPath, admissionPath };
}

function assertTransient(decision: ReturnType<typeof eliteAdmissionFromState>, reason: string) {
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, reason);
  assert.equal(decision.disposition, 'TRANSIENT');
  assert.equal(decision.retryable, true);
  assert.equal(shouldPersistAdmissionDenial(decision), false);
}

test('compact selector-index corruption is transient and cannot consume NEW/ADD', () => {
  for (const [name, body, reason] of [
    ['unparseable', '{broken', 'candidate_snapshot_index_unparseable'],
    ['wrapper', JSON.stringify({ version: 2, rows: [] }), 'candidate_snapshot_index_invalid_wrapper'],
    ['row', JSON.stringify({ version: 1, rows: [{}] }), 'candidate_snapshot_index_invalid_row'],
  ] as const) {
    const { statePath, snapshotsPath, admissionPath } = setup();
    writeFileSync(`${snapshotsPath}.recent.json`, body);
    const decision = eliteAdmissionFromState(
      statePath, portfolioId, now, 20 * 60_000, snapshotsPath, admissionPath,
    );
    assertTransient(decision, reason);
    assert.ok(name);
  }

  const { statePath, snapshotsPath, admissionPath } = setup();
  writeFileSync(`${snapshotsPath}.recent.json`, '');
  truncateSync(`${snapshotsPath}.recent.json`, 8 * 1024 * 1024 + 1);
  assertTransient(
    eliteAdmissionFromState(statePath, portfolioId, now, 20 * 60_000, snapshotsPath, admissionPath),
    'candidate_snapshot_index_oversize',
  );
});

test('present corrupt admission row is transient but absent healthy row is terminal', () => {
  const corruptRows: unknown[] = [
    null,
    {},
    { portfolioId: 'wrong', selectorVersion: ELITE_SELECTOR_VERSION, admittedAtMs: now - 1 },
    { portfolioId, selectorVersion: 'wrong-selector', admittedAtMs: now - 1 },
    { portfolioId, selectorVersion: ELITE_SELECTOR_VERSION, admittedAtMs: 'bad' },
    { portfolioId, selectorVersion: ELITE_SELECTOR_VERSION, admittedAtMs: now + 1 },
  ];
  for (const row of corruptRows) {
    const { statePath, admissionPath } = setup();
    writeFileSync(admissionPath, JSON.stringify({
      version: 1, generatedAtMs: now, healthy: true, suspensionReason: null,
      rows: { [portfolioId]: row },
    }));
    assertTransient(
      eliteAdmissionFromState(statePath, portfolioId, now, 20 * 60_000, undefined, admissionPath),
      'direct_watch_admission_index_invalid',
    );
  }

  const { statePath, admissionPath } = setup();
  writeFileSync(admissionPath, JSON.stringify({
    version: 1, generatedAtMs: now, healthy: true, suspensionReason: null, rows: {},
  }));
  const absent = eliteAdmissionFromState(
    statePath, portfolioId, now, 20 * 60_000, undefined, admissionPath,
  );
  assert.equal(absent.reason, 'direct_watch_not_admitted');
  assert.equal(absent.disposition, 'TERMINAL');
  assert.equal(absent.retryable, false);
  assert.equal(shouldPersistAdmissionDenial(absent), true);
});
