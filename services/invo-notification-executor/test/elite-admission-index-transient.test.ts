import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, truncateSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { eliteAdmissionFromState, shouldPersistAdmissionDenial } from '../src/elite-admission.js';
import { DEFAULT_PORTFOLIO_SELECTOR, ELITE_SELECTOR_VERSION } from '../src/portfolio-candidates.js';
import { FEED_EVIDENCE_EPOCH } from '../src/feed-portfolio-evidence.js';

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
    policy: DEFAULT_PORTFOLIO_SELECTOR,
    feedEvidence: { version: 4, epoch: FEED_EVIDENCE_EPOCH, eligibilityNotBeforeMs: observedAtMs,
      records: {}, lifetime: { discovered: 0, feedOnly: 0, newlyQualified: 0, rejectedUnverified: 0,
        rejectedMalformed: 0, rejectedIdentityConflicts: 0, dedupedReplayCount: 0 } },
    lastObservedAtMs: observedAtMs,
    firstEliteAtMs: { [portfolioId]: observedAtMs },
    portfolios: {
      [portfolioId]: {
        portfolioId,
        selectorVersion: ELITE_SELECTOR_VERSION,
        observedAtMs,
        bucket: 'ELITE_CANDIDATE',
        portfolioName: 'Elite', ownerId: 'owner', username: 'elite', verified: true,
        createdAtMs: observedAtMs - 10 * 86_400_000, lastActivityAtMs: observedAtMs,
        openPositions: 1, wonPositions: 20, lostPositions: 5,
        closedPositions: 25,
        closedPositionsPerDay: 2,
        winRatePct: 80,
        percentChange: 200,
        hybridRequiredReturnPct: 100, hybridRequiredClosedPositions: 20,
        winLossRatio: 4,
        currentWinStreak: 2, followerCount: 10,
        daysActive: 10,
        recentActivityDaysAgo: 0,
        liquidated: false,
        sourceFilter: 'test',
        score: 90,
        scoreBreakdown: { winRate: 20, historicalReturn: 20, sampleSize: 10,
          activeDays: 5, dailyFrequency: 5, recentActivity: 5, availableWeight: 100 },
        reasons: ['qualified'], rawShapeKeys: ['id'],
      },
    },
  }));
  writeFileSync(admissionPath, JSON.stringify({
    version: 2,
    generatedAtMs: now,
    healthy: true,
    suspensionReason: null,
    rows: {
      [portfolioId]: {
        portfolioId,
        selectorVersion: ELITE_SELECTOR_VERSION,
        intervals: [{ admittedAtMs: observedAtMs, admittedUntilMs: null }],
        score: 90,
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
    ['row', JSON.stringify({ version: 1, selectorVersion: ELITE_SELECTOR_VERSION, rows: [{}] }),
      'candidate_snapshot_index_invalid_row'],
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
    { portfolioId: 'wrong', selectorVersion: ELITE_SELECTOR_VERSION,
      intervals: [{ admittedAtMs: now - 1, admittedUntilMs: null }], score: 90 },
    { portfolioId, selectorVersion: 'wrong-selector',
      intervals: [{ admittedAtMs: now - 1, admittedUntilMs: null }], score: 90 },
    { portfolioId, selectorVersion: ELITE_SELECTOR_VERSION,
      intervals: [{ admittedAtMs: 'bad', admittedUntilMs: null }], score: 90 },
    { portfolioId, selectorVersion: ELITE_SELECTOR_VERSION,
      intervals: [{ admittedAtMs: now + 1, admittedUntilMs: null }], score: 90 },
  ];
  for (const row of corruptRows) {
    const { statePath, admissionPath } = setup();
    writeFileSync(admissionPath, JSON.stringify({
      version: 2, generatedAtMs: now, healthy: true, suspensionReason: null,
      rows: { [portfolioId]: row },
    }));
    assertTransient(
      eliteAdmissionFromState(statePath, portfolioId, now, 20 * 60_000, undefined, admissionPath),
      'direct_watch_admission_index_invalid',
    );
  }

  const { statePath, admissionPath } = setup();
  writeFileSync(admissionPath, JSON.stringify({
    version: 2, generatedAtMs: now, healthy: true, suspensionReason: null, rows: {},
  }));
  const absent = eliteAdmissionFromState(
    statePath, portfolioId, now, 20 * 60_000, undefined, admissionPath,
  );
  assert.equal(absent.reason, 'direct_watch_not_admitted');
  assert.equal(absent.disposition, 'TERMINAL');
  assert.equal(absent.retryable, false);
  assert.equal(shouldPersistAdmissionDenial(absent), true);
});


test('primary candidate-state missing or corruption is transient and cannot consume NEW/ADD', () => {
  {
    const { statePath, admissionPath } = setup();
    unlinkSync(statePath);
    assertTransient(
      eliteAdmissionFromState(statePath, portfolioId, now, 20 * 60_000, undefined, admissionPath),
      'candidate_state_missing',
    );
  }
  for (const [body, reason] of [
    ['{broken', 'candidate_state_unparseable'],
    [JSON.stringify([]), 'candidate_state_invalid_envelope'],
    [JSON.stringify({ selectorVersion: ELITE_SELECTOR_VERSION, lastObservedAtMs: now, portfolios: [] }),
      'candidate_state_invalid_envelope'],
    [JSON.stringify({ selectorVersion: ELITE_SELECTOR_VERSION, lastObservedAtMs: now, portfolios: {}, firstEliteAtMs: [] }),
      'candidate_state_invalid_envelope'],
  ] as const) {
    const { statePath, admissionPath } = setup();
    writeFileSync(statePath, body);
    assertTransient(
      eliteAdmissionFromState(statePath, portfolioId, now, 20 * 60_000, undefined, admissionPath),
      reason,
    );
  }
});

test('stale/version-incompatible selector state is transient but valid non-elite evidence is terminal', () => {
  const cases: Array<[any, string]> = [
    [{ selectorVersion: 'old-selector' }, 'candidate_selector_version_mismatch'],
    [{ selectorVersion: null }, 'candidate_selector_version_missing'],
    [{ lastObservedAtMs: now - 21 * 60_000 }, 'candidate_state_stale'],
  ];
  for (const [patch, reason] of cases) {
    const { statePath, admissionPath } = setup();
    const state = JSON.parse(readFileSync(statePath, 'utf8'));
    writeFileSync(statePath, JSON.stringify({ ...state, ...patch }));
    assertTransient(
      eliteAdmissionFromState(statePath, portfolioId, now, 20 * 60_000, undefined, admissionPath),
      reason,
    );
  }

  const { statePath, admissionPath } = setup();
  const state = JSON.parse(readFileSync(statePath, 'utf8'));
  state.portfolios[portfolioId].bucket = 'RESEARCH_WIDE';
  writeFileSync(statePath, JSON.stringify(state));
  const terminal = eliteAdmissionFromState(statePath, portfolioId, now, 20 * 60_000, undefined, admissionPath);
  assert.equal(terminal.reason, 'portfolio_not_elite');
  assert.equal(terminal.disposition, 'TERMINAL');
  assert.equal(shouldPersistAdmissionDenial(terminal), true);
});
