import assert from 'node:assert/strict';
import { mkdtempSync, truncateSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { eliteAdmissionFromState, shouldPersistAdmissionDenial } from '../src/elite-admission.js';
import { DEFAULT_PORTFOLIO_SELECTOR, ELITE_SELECTOR_VERSION } from '../src/portfolio-candidates.js';
import { FEED_EVIDENCE_EPOCH } from '../src/feed-portfolio-evidence.js';

function stateFile(state: unknown) {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-elite-admission-'));
  const path = join(dir, 'portfolio-candidates.json');
  writeFileSync(path, JSON.stringify(state));
  return path;
}

function admissionIndex(statePath: string, admittedAtMs = candidateObservedAtMs - 1_000) {
  const path = join(statePath, '..', 'elite-direct-watch-admissions.json');
  writeFileSync(path, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs,
    healthy: true, suspensionReason: null, rows: {
    [portfolioId]: { portfolioId, intervals: [{ admittedAtMs, admittedUntilMs: null }], score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
  } }));
  return path;
}

const decisionAtMs = Date.UTC(2026, 8, 16, 12, 0, 0);
const candidateObservedAtMs = decisionAtMs - 60_000;
const portfolioId = 'elite-portfolio';

function validState(overrides: Record<string, unknown> = {}) {
  return {
    version: 1,
    selectorVersion: ELITE_SELECTOR_VERSION,
    policy: DEFAULT_PORTFOLIO_SELECTOR,
    feedEvidence: { version: 4, epoch: FEED_EVIDENCE_EPOCH, eligibilityNotBeforeMs: candidateObservedAtMs,
      records: {}, lifetime: { discovered: 0, feedOnly: 0, newlyQualified: 0, rejectedUnverified: 0,
        rejectedMalformed: 0, rejectedIdentityConflicts: 0, dedupedReplayCount: 0 } },
    lastObservedAtMs: candidateObservedAtMs,
    firstEliteAtMs: { [portfolioId]: candidateObservedAtMs - 1_000 },
    portfolios: {
      [portfolioId]: {
        portfolioId,
        observedAtMs: candidateObservedAtMs,
        portfolioName: 'Elite portfolio',
        ownerId: 'owner-1',
        username: 'elite-owner',
        verified: true,
        createdAtMs: candidateObservedAtMs - 8 * 86_400_000,
        lastActivityAtMs: candidateObservedAtMs - 1_000,
        openPositions: 1,
        bucket: 'ELITE_CANDIDATE',
        closedPositions: 24,
        closedPositionsPerDay: 3,
        winRatePct: 83.3,
        percentChange: 120,
        hybridRequiredReturnPct: 80,
        hybridRequiredClosedPositions: 20,
        winLossRatio: 4.99,
        wonPositions: 20,
        lostPositions: 4,
        currentWinStreak: 3,
        followerCount: 10,
        daysActive: 8,
        recentActivityDaysAgo: 0.25,
        liquidated: false,
        sourceFilter: '1W',
        score: 63.4,
        scoreBreakdown: { winRate: 20, historicalReturn: 20, sampleSize: 10,
          activeDays: 5, dailyFrequency: 5, recentActivity: 3, availableWeight: 100 },
        selectorVersion: ELITE_SELECTOR_VERSION,
        reasons: ['meets_hybrid_win_rate_return_gate_v3'],
        rawShapeKeys: ['id'],
      },
    },
    ...overrides,
  };
}

test('admits only a portfolio proven elite under the exact current selector before the decision', () => {
  const path = stateFile(validState());
  const decision = eliteAdmissionFromState(path, portfolioId, decisionAtMs, 20 * 60_000, undefined, admissionIndex(path));
  assert.equal(decision.allowed, true);
  assert.equal(decision.reason, 'elite_candidate_pretrade_qualified');
  assert.equal(decision.portfolioId, portfolioId);
  assert.equal(decision.closedPositions, 24);
  assert.equal(decision.winRatePct, 83.3);
  assert.equal(decision.selectorVersion, ELITE_SELECTOR_VERSION);
});

test('obsolete v2 selector state cannot authorize v3 NEW/ADD exposure', () => {
  const state = validState({ selectorVersion: 'invo-portfolio-elite-v2-20260916' });
  const decision = eliteAdmissionFromState(stateFile(state), portfolioId, decisionAtMs, 20 * 60_000);
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, 'candidate_selector_version_mismatch');
  assert.equal(decision.selectorVersion, 'invo-portfolio-elite-v2-20260916');
});

test('unknown selector state cannot authorize NEW/ADD exposure', () => {
  const state = validState({ selectorVersion: 'unknown-selector' });
  const decision = eliteAdmissionFromState(stateFile(state), portfolioId, decisionAtMs, 20 * 60_000);
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, 'candidate_selector_version_mismatch');
});

test('full candidate envelope corruption is transient and non-consumable', () => {
  const cases: any[] = [];
  const missingPolicy = validState(); delete (missingPolicy as any).policy; cases.push(missingPolicy);
  cases.push(validState({ policy: { ...DEFAULT_PORTFOLIO_SELECTOR, minClosedPositions: '20' } }));
  cases.push(validState({ lastObservedAtMs: String(candidateObservedAtMs) }));
  cases.push(validState({ version: 2 }));
  cases.push(validState({ feedEvidence: { version: 3 } }));
  cases.push(validState({ feedEvidence: { ...(validState() as any).feedEvidence, records: [] } }));
  cases.push({ ...validState(), unexpectedRequiredStructure: {} });
  for (const state of cases) {
    const decision = eliteAdmissionFromState(stateFile(state), portfolioId, decisionAtMs, 20 * 60_000);
    assert.equal(decision.allowed, false); assert.equal(decision.disposition, 'TRANSIENT');
    assert.equal(decision.retryable, true); assert.equal(shouldPersistAdmissionDenial(decision), false);
  }
});

test('leaderboard/discovery presence without elite bucket is rejected', () => {
  const state = validState({
    portfolios: {
      [portfolioId]: {
        ...(validState().portfolios as any)[portfolioId],
        bucket: 'RESEARCH_WIDE',
      },
    },
  });
  const decision = eliteAdmissionFromState(stateFile(state), portfolioId, decisionAtMs, 20 * 60_000);
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, 'portfolio_not_elite');
});

test('future elite observation cannot authorize an earlier trade', () => {
  const state = validState({
    lastObservedAtMs: decisionAtMs - 10_000,
    firstEliteAtMs: { [portfolioId]: decisionAtMs + 1 },
  });
  const decision = eliteAdmissionFromState(stateFile(state), portfolioId, decisionAtMs, 20 * 60_000);
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, 'portfolio_not_elite_at_decision_time');
});

test('stale candidate state fails closed', () => {
  const state = validState({ lastObservedAtMs: decisionAtMs - 21 * 60_000 });
  const decision = eliteAdmissionFromState(stateFile(state), portfolioId, decisionAtMs, 20 * 60_000);
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, 'candidate_state_stale');
});

test('stale portfolio observation fails closed even when aggregate candidate state is fresh', () => {
  const state = validState({
    lastObservedAtMs: decisionAtMs - 60_000,
    portfolios: {
      [portfolioId]: {
        ...(validState().portfolios as any)[portfolioId],
        observedAtMs: decisionAtMs - 21 * 60_000,
      },
    },
  });
  const decision = eliteAdmissionFromState(stateFile(state), portfolioId, decisionAtMs, 20 * 60_000);
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, 'candidate_observation_stale');
  assert.equal(decision.candidateStateLastObservedAtMs, decisionAtMs - 60_000);
  assert.equal(decision.candidateObservedAtMs, decisionAtMs - 21 * 60_000);
});

test('missing candidate state fails closed', () => {
  const decision = eliteAdmissionFromState('/does/not/exist/portfolio-candidates.json', portfolioId, decisionAtMs, 20 * 60_000);
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, 'candidate_state_missing');
});


test('admission index requires a structurally valid healthy fresh envelope', () => {
  const path = stateFile(validState());
  const index = admissionIndex(path);
  const maxIndexAgeMs = 10_000;

  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs, rows: {
    [portfolioId]: { portfolioId, intervals: [{ admittedAtMs: candidateObservedAtMs - 1_000, admittedUntilMs: null }],
      score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
  } }));
  const missingHealthy = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, undefined, index, maxIndexAgeMs,
  );
  assert.equal(missingHealthy.allowed, false);
  assert.equal(missingHealthy.disposition, 'TRANSIENT');
  assert.equal(missingHealthy.reason, 'direct_watch_admission_index_invalid');
  assert.equal(shouldPersistAdmissionDenial(missingHealthy), false);

  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs - maxIndexAgeMs - 1,
    healthy: true, suspensionReason: null, rows: {
      [portfolioId]: { portfolioId, intervals: [{ admittedAtMs: candidateObservedAtMs - 1_000, admittedUntilMs: null }],
        score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
    } }));
  const stale = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, undefined, index, maxIndexAgeMs,
  );
  assert.equal(stale.allowed, false);
  assert.equal(stale.disposition, 'TRANSIENT');
  assert.equal(stale.reason, 'direct_watch_admission_index_stale');

  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs + 1,
    healthy: true, suspensionReason: null, rows: {
      [portfolioId]: { portfolioId, intervals: [{ admittedAtMs: candidateObservedAtMs - 1_000, admittedUntilMs: null }],
        score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
    } }));
  const future = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, undefined, index, maxIndexAgeMs,
  );
  assert.equal(future.allowed, false);
  assert.equal(future.disposition, 'TRANSIENT');
  assert.equal(future.reason, 'direct_watch_admission_index_from_future');

  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs,
    healthy: true, suspensionReason: 'contradictory', rows: {
      [portfolioId]: { portfolioId, intervals: [{ admittedAtMs: candidateObservedAtMs - 1_000, admittedUntilMs: null }],
        score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
    } }));
  const contradictory = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, undefined, index, maxIndexAgeMs,
  );
  assert.equal(contradictory.allowed, false);
  assert.equal(contradictory.reason, 'direct_watch_admission_index_invalid');
});

test('qualified candidate is waitlisted until direct-watch admission and cannot replay pre-admission feed events', () => {
  const path = stateFile(validState());
  const waitlisted = eliteAdmissionFromState(path, portfolioId, decisionAtMs, 20 * 60_000);
  assert.equal(waitlisted.allowed, false);
  assert.equal(waitlisted.reason, 'direct_watch_admission_index_missing');

  const index = join(path, '..', 'elite-direct-watch-admissions.json');
  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs,
    healthy: true, suspensionReason: null, rows: {} }));
  const preAdmission = eliteAdmissionFromState(path, portfolioId, decisionAtMs, 20 * 60_000, undefined, index);
  assert.equal(preAdmission.allowed, false);
  assert.equal(preAdmission.reason, 'direct_watch_not_admitted');
  assert.equal(preAdmission.disposition, 'TERMINAL');

  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs + 10,
    healthy: true, suspensionReason: null, rows: {
      [portfolioId]: { portfolioId, intervals: [{ admittedAtMs: decisionAtMs + 10, admittedUntilMs: null }],
        score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
    } }));
  const prospective = eliteAdmissionFromState(path, portfolioId, decisionAtMs + 11, 20 * 60_000, undefined, index);
  assert.equal(prospective.allowed, true);
  assert.equal(prospective.directWatchAdmittedAtMs, decisionAtMs + 10);
});

test('delayed pre-promotion trade stays rejected at its source-time boundary', () => {
  const path = stateFile(validState({
    lastObservedAtMs: decisionAtMs + 20_000,
    firstEliteAtMs: { [portfolioId]: decisionAtMs + 10_000 },
    portfolios: { [portfolioId]: {
      ...(validState().portfolios as any)[portfolioId], observedAtMs: decisionAtMs + 20_000,
    } },
  }));
  const index = admissionIndex(path, decisionAtMs + 15_000);
  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs + 30_000,
    healthy: true, suspensionReason: null, rows: {
      [portfolioId]: { portfolioId, intervals: [{ admittedAtMs: decisionAtMs + 15_000, admittedUntilMs: null }],
        score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
    } }));
  const result = eliteAdmissionFromState(path, portfolioId, decisionAtMs, 20 * 60_000,
    undefined, index, 60_000, decisionAtMs + 30_000);
  assert.equal(result.allowed, false);
  assert.equal(result.reason, 'candidate_observation_from_future');
});

test('trade whose own later outcome causes promotion cannot gain admission', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-own-outcome-'));
  const path = join(dir, 'portfolio-candidates.json');
  const snapshots = join(dir, 'portfolio-candidate-snapshots.jsonl');
  writeFileSync(path, JSON.stringify(validState({ lastObservedAtMs: decisionAtMs + 20_000,
    firstEliteAtMs: { [portfolioId]: decisionAtMs + 20_000 } })));
  writeFileSync(`${snapshots}.recent.json`, JSON.stringify({ version: 1,
    selectorVersion: ELITE_SELECTOR_VERSION, rows: [{
      ...(validState().portfolios as any)[portfolioId], observedAtMs: decisionAtMs - 1_000,
      bucket: 'RESEARCH_WIDE',
    }] }));
  const index = join(dir, 'elite-direct-watch-admissions.json');
  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs + 30_000,
    healthy: true, suspensionReason: null, rows: {
      [portfolioId]: { portfolioId,
        intervals: [{ admittedAtMs: decisionAtMs + 20_000, admittedUntilMs: null }],
        score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
    } }));
  const result = eliteAdmissionFromState(path, portfolioId, decisionAtMs, 20 * 60_000,
    snapshots, index, 60_000, decisionAtMs + 30_000);
  assert.equal(result.allowed, false);
  assert.equal(result.reason, 'portfolio_not_elite');
});

test('selected at source then demoted before processing uses historical selector and admission interval', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-source-selected-'));
  const path = join(dir, 'portfolio-candidates.json');
  const snapshots = join(dir, 'portfolio-candidate-snapshots.jsonl');
  writeFileSync(path, JSON.stringify(validState({ lastObservedAtMs: decisionAtMs + 20_000,
    portfolios: { [portfolioId]: {
      ...(validState().portfolios as any)[portfolioId], observedAtMs: decisionAtMs + 20_000,
      bucket: 'REJECTED_DEMOTED',
    } } })));
  writeFileSync(`${snapshots}.recent.json`, JSON.stringify({ version: 1,
    selectorVersion: ELITE_SELECTOR_VERSION, rows: [{
      ...(validState().portfolios as any)[portfolioId], observedAtMs: decisionAtMs - 1_000,
    }] }));
  const index = join(dir, 'elite-direct-watch-admissions.json');
  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs + 30_000,
    healthy: true, suspensionReason: null, rows: {
      [portfolioId]: { portfolioId, intervals: [{ admittedAtMs: decisionAtMs - 5_000,
        admittedUntilMs: decisionAtMs + 10_000 }], score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
    } }));
  const first = eliteAdmissionFromState(path, portfolioId, decisionAtMs, 20 * 60_000,
    snapshots, index, 60_000, decisionAtMs + 30_000);
  const retry = eliteAdmissionFromState(path, portfolioId, decisionAtMs, 20 * 60_000,
    snapshots, index, 60_000, decisionAtMs + 35_000);
  assert.equal(first.allowed, true);
  assert.equal(retry.allowed, true);
  assert.equal(retry.reason, first.reason);
});

test('multiple admission intervals preserve retries and reject the demotion gap', () => {
  const path = stateFile(validState());
  const index = join(path, '..', 'elite-direct-watch-admissions.json');
  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs + 40_000,
    healthy: true, suspensionReason: null, rows: {
      [portfolioId]: { portfolioId, intervals: [
        { admittedAtMs: decisionAtMs - 20_000, admittedUntilMs: decisionAtMs - 10_000 },
        { admittedAtMs: decisionAtMs + 10_000, admittedUntilMs: null },
      ], score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
    } }));
  const evaluate = (sourceTimeMs: number, retryAtMs = decisionAtMs + 40_000) => eliteAdmissionFromState(
    path, portfolioId, sourceTimeMs, 20 * 60_000, undefined, index, 60_000, retryAtMs,
  );
  assert.equal(evaluate(decisionAtMs - 15_000).allowed, true);
  assert.equal(evaluate(decisionAtMs - 15_000, decisionAtMs + 45_000).allowed, true,
    'a retry resolves against the same historical interval');
  assert.equal(evaluate(decisionAtMs).reason, 'direct_watch_not_admitted_at_signal_time');
  assert.equal(evaluate(decisionAtMs + 20_000).allowed, true);
});

test('malformed admission interval history fails closed as transient corruption', () => {
  const path = stateFile(validState());
  const index = join(path, '..', 'elite-direct-watch-admissions.json');
  const malformed = [
    [],
    [{ admittedAtMs: decisionAtMs - 10, admittedUntilMs: null },
      { admittedAtMs: decisionAtMs + 10, admittedUntilMs: null }],
    [{ admittedAtMs: decisionAtMs, admittedUntilMs: decisionAtMs - 1 }],
    [{ admittedAtMs: decisionAtMs - 10, admittedUntilMs: decisionAtMs + 10 },
      { admittedAtMs: decisionAtMs, admittedUntilMs: null }],
  ];
  for (const intervals of malformed) {
    writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs + 40_000,
      healthy: true, suspensionReason: null, rows: {
        [portfolioId]: { portfolioId, intervals, score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
      } }));
    const result = eliteAdmissionFromState(path, portfolioId, decisionAtMs, 20 * 60_000,
      undefined, index, 60_000, decisionAtMs + 40_000);
    assert.equal(result.reason, 'direct_watch_admission_index_invalid');
    assert.equal(result.disposition, 'TRANSIENT');
  }
});

test('valid admission history is not rejected by an arbitrary interval-count limit', () => {
  const path = stateFile(validState());
  const index = join(path, '..', 'elite-direct-watch-admissions.json');
  const intervals = Array.from({ length: 17 }, (_, offset) => ({
    admittedAtMs: decisionAtMs - 100 + offset * 2,
    admittedUntilMs: decisionAtMs - 99 + offset * 2,
  }));
  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs + 40_000,
    healthy: true, suspensionReason: null, rows: {
      [portfolioId]: { portfolioId, intervals, score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
    } }));
  const result = eliteAdmissionFromState(path, portfolioId, decisionAtMs - 100,
    20 * 60_000, undefined, index, 60_000, decisionAtMs + 40_000);
  assert.equal(result.allowed, true);
  assert.equal(result.reason, 'elite_candidate_pretrade_qualified');
});

test('admission timestamp after index generation is transient corruption and never consumable', () => {
  const path = stateFile(validState());
  const index = join(path, '..', 'elite-direct-watch-admissions.json');
  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs,
    healthy: true, suspensionReason: null, rows: {
      [portfolioId]: { portfolioId, intervals: [{ admittedAtMs: decisionAtMs + 1, admittedUntilMs: null }],
        score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
    } }));
  const decision = eliteAdmissionFromState(path, portfolioId, decisionAtMs, 20 * 60_000, undefined, index);
  assert.equal(decision.reason, 'direct_watch_admission_index_invalid');
  assert.equal(decision.disposition, 'TRANSIENT');
  assert.equal(decision.retryable, true);
  assert.equal(shouldPersistAdmissionDenial(decision), false);
});

test('feed NEW during scan suspension remains unseen and cursor-safe, then executes once after health returns', () => {
  const path = stateFile(validState());
  const index = admissionIndex(path);
  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs,
    healthy: false, suspensionReason: 'scan_in_progress', rows: {} }));
  const duringScan = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, undefined, index,
  );
  let seen = false; let cursorAdvanced = false; let executions = 0;
  if (shouldPersistAdmissionDenial(duringScan)) seen = true;
  if (seen) cursorAdvanced = true;
  assert.equal(duringScan.disposition, 'TRANSIENT');
  assert.equal(duringScan.retryable, true);
  assert.equal(seen, false);
  assert.equal(cursorAdvanced, false);

  writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs + 1,
    healthy: true, suspensionReason: null, rows: {
      [portfolioId]: { portfolioId, intervals: [{ admittedAtMs: candidateObservedAtMs - 1_000, admittedUntilMs: null }],
        score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
    } }));
  const recovered = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs + 2, 20 * 60_000, undefined, index,
  );
  if (recovered.allowed && !seen) { executions += 1; seen = true; }
  if (seen) cursorAdvanced = true;
  assert.equal(recovered.disposition, 'ALLOWED');
  assert.equal(executions, 1);
  assert.equal(cursorAdvanced, true);
  if (recovered.allowed && !seen) executions += 1;
  assert.equal(executions, 1);
});

test('structural non-elite admission remains terminal', () => {
  const state = validState({ portfolios: { [portfolioId]: {
    ...(validState().portfolios as any)[portfolioId], bucket: 'RESEARCH_WIDE',
  } } });
  const decision = eliteAdmissionFromState(stateFile(state), portfolioId, decisionAtMs, 20 * 60_000);
  assert.equal(decision.disposition, 'TERMINAL');
  assert.equal(shouldPersistAdmissionDenial(decision), true);
});

test('future aggregate state stays transient even when a historical row is pre-trade', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-elite-history-'));
  const statePath = join(dir, 'portfolio-candidates.json');
  const snapshotsPath = join(dir, 'portfolio-candidate-snapshots.jsonl');
  const historicalAt = decisionAtMs - 5 * 60_000;
  const futureAt = decisionAtMs + 60_000;
  writeFileSync(statePath, JSON.stringify(validState({
    lastObservedAtMs: futureAt,
    firstEliteAtMs: { [portfolioId]: decisionAtMs - 10 * 60_000 },
    portfolios: {
      [portfolioId]: {
        ...(validState().portfolios as any)[portfolioId],
        observedAtMs: futureAt,
        bucket: 'ELITE_CANDIDATE',
      },
    },
  })));
  writeFileSync(`${snapshotsPath}.recent.json`, JSON.stringify({ version: 1,
    selectorVersion: ELITE_SELECTOR_VERSION, rows: [{
    ...(validState().portfolios as any)[portfolioId],
    selectorVersion: ELITE_SELECTOR_VERSION,
    observedAtMs: historicalAt,
    bucket: 'ELITE_CANDIDATE',
  }] }));
  const decision = eliteAdmissionFromState(
    statePath, portfolioId, decisionAtMs, 20 * 60_000, snapshotsPath, admissionIndex(statePath),
  );
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, 'candidate_state_from_future');
  assert.equal(decision.disposition, 'TRANSIENT');
  assert.equal(shouldPersistAdmissionDenial(decision), false);
});

test('latest pre-trade snapshot preserves demotion and prevents look-ahead re-promotion', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-elite-history-demotion-'));
  const statePath = join(dir, 'portfolio-candidates.json');
  const snapshotsPath = join(dir, 'portfolio-candidate-snapshots.jsonl');
  const eliteAt = decisionAtMs - 10 * 60_000;
  const demotedAt = decisionAtMs - 60_000;
  const futureAt = decisionAtMs + 60_000;
  writeFileSync(statePath, JSON.stringify(validState({
    lastObservedAtMs: decisionAtMs,
    firstEliteAtMs: { [portfolioId]: eliteAt },
    portfolios: {
      [portfolioId]: {
        ...(validState().portfolios as any)[portfolioId],
        observedAtMs: futureAt,
        bucket: 'ELITE_CANDIDATE',
      },
    },
  })));
  const baseCandidate = (validState().portfolios as any)[portfolioId];
  writeFileSync(`${snapshotsPath}.recent.json`, JSON.stringify({ version: 1,
    selectorVersion: ELITE_SELECTOR_VERSION, rows: [
    { ...baseCandidate, selectorVersion: ELITE_SELECTOR_VERSION, observedAtMs: eliteAt, bucket: 'ELITE_CANDIDATE' },
    { ...baseCandidate, selectorVersion: ELITE_SELECTOR_VERSION, observedAtMs: demotedAt, bucket: 'RESEARCH_WIDE' },
  ] }));
  const decision = eliteAdmissionFromState(
    statePath, portfolioId, decisionAtMs, 20 * 60_000, snapshotsPath,
  );
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, 'portfolio_not_elite');
  assert.equal(decision.candidateObservedAtMs, demotedAt);
});

test('admission latency and memory are independent of a sparse 1GB append-only history', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-elite-billion-'));
  const statePath = join(dir, 'portfolio-candidates.json');
  const snapshotsPath = join(dir, 'portfolio-candidate-snapshots.jsonl');
  writeFileSync(statePath, JSON.stringify(validState()));
  writeFileSync(snapshotsPath, '');
  truncateSync(snapshotsPath, 1024 * 1024 * 1024);
  const candidate = (validState().portfolios as any)[portfolioId];
  writeFileSync(`${snapshotsPath}.recent.json`, JSON.stringify({ version: 1,
    selectorVersion: ELITE_SELECTOR_VERSION, rows: [
    { ...candidate, selectorVersion: ELITE_SELECTOR_VERSION },
  ] }));
  const before = process.memoryUsage().heapUsed;
  const decision = eliteAdmissionFromState(statePath, portfolioId, decisionAtMs, 20 * 60_000, snapshotsPath,
    admissionIndex(statePath));
  assert.equal(decision.allowed, true);
  assert.ok(process.memoryUsage().heapUsed - before < 8 * 1024 * 1024);
});

test('malformed compact snapshot index fails closed instead of throwing through execute', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-elite-malformed-index-'));
  const statePath = join(dir, 'portfolio-candidates.json');
  const snapshotsPath = join(dir, 'portfolio-candidate-snapshots.jsonl');
  writeFileSync(statePath, JSON.stringify(validState()));
  writeFileSync(`${snapshotsPath}.recent.json`, '{"version":1,"rows":[');
  const decision = eliteAdmissionFromState(statePath, portfolioId, decisionAtMs, 20 * 60_000, snapshotsPath);
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, 'candidate_snapshot_index_unparseable');
  assert.equal(decision.disposition, 'TRANSIENT');
  assert.equal(decision.retryable, true);
  assert.equal(shouldPersistAdmissionDenial(decision), false);
});

test('invalid compact index rows fail closed without falling back to current elite state', () => {
  const badRows = [
    [{}],
    [{ ...(validState().portfolios as any)[portfolioId], selectorVersion: 'old-selector' }],
    [
      { ...(validState().portfolios as any)[portfolioId], selectorVersion: ELITE_SELECTOR_VERSION },
      {},
    ],
    [{ ...(validState().portfolios as any)[portfolioId], selectorVersion: ELITE_SELECTOR_VERSION,
      observedAtMs: 'not-a-timestamp' }],
    [{ ...(validState().portfolios as any)[portfolioId], selectorVersion: ELITE_SELECTOR_VERSION,
      observedAtMs: String(candidateObservedAtMs) }],
    [{ ...(validState().portfolios as any)[portfolioId], selectorVersion: ELITE_SELECTOR_VERSION,
      observedAtMs: 0 }],
    [{ ...(validState().portfolios as any)[portfolioId], selectorVersion: ELITE_SELECTOR_VERSION,
      bucket: 'NOT_A_BUCKET' }],
  ];
  for (const [index, rows] of badRows.entries()) {
    const dir = mkdtempSync(join(tmpdir(), `lane3-elite-invalid-row-${index}-`));
    const statePath = join(dir, 'portfolio-candidates.json');
    const snapshotsPath = join(dir, 'portfolio-candidate-snapshots.jsonl');
    writeFileSync(statePath, JSON.stringify(validState()));
    writeFileSync(`${snapshotsPath}.recent.json`, JSON.stringify({ version: 1,
      selectorVersion: ELITE_SELECTOR_VERSION, rows }));
    const decision = eliteAdmissionFromState(
      statePath, portfolioId, decisionAtMs, 20 * 60_000, snapshotsPath,
    );
    assert.equal(decision.allowed, false, `case ${index} must fail closed`);
    assert.equal(decision.reason, 'candidate_snapshot_index_invalid_row');
    assert.equal(decision.disposition, 'TRANSIENT');
    assert.equal(decision.retryable, true);
    assert.equal(shouldPersistAdmissionDenial(decision), false);
  }
});

test('invalid compact index wrapper fails closed without current-state fallback', () => {
  for (const wrapper of [[], { version: 2, rows: [] }, { version: 1, rows: {} }]) {
    const dir = mkdtempSync(join(tmpdir(), 'lane3-elite-invalid-wrapper-'));
    const statePath = join(dir, 'portfolio-candidates.json');
    const snapshotsPath = join(dir, 'portfolio-candidate-snapshots.jsonl');
    writeFileSync(statePath, JSON.stringify(validState()));
    writeFileSync(`${snapshotsPath}.recent.json`, JSON.stringify(wrapper));
    const decision = eliteAdmissionFromState(
      statePath, portfolioId, decisionAtMs, 20 * 60_000, snapshotsPath,
    );
    assert.equal(decision.allowed, false);
    assert.equal(decision.reason, 'candidate_snapshot_index_invalid_wrapper');
    assert.equal(decision.disposition, 'TRANSIENT');
    assert.equal(decision.retryable, true);
    assert.equal(shouldPersistAdmissionDenial(decision), false);
  }
});

test('malformed existing admission row is transient corruption and cannot consume NEW/ADD', () => {
  const path = stateFile(validState());
  const index = admissionIndex(path);
  writeFileSync(index, JSON.stringify({
    version: 2, generatedAtMs: decisionAtMs, healthy: true, suspensionReason: null,
    rows: { [portfolioId]: { portfolioId, intervals: [{ admittedAtMs: candidateObservedAtMs - 1_000, admittedUntilMs: null }],
      selectorVersion: 'wrong-selector' } },
  }));
  const decision = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, undefined, index,
  );
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, 'direct_watch_admission_index_invalid');
  assert.equal(decision.disposition, 'TRANSIENT');
  assert.equal(decision.retryable, true);
  assert.equal(shouldPersistAdmissionDenial(decision), false);
});

test('every canonical candidate field is required before elite allow or non-elite terminal denial', () => {
  const canonical = (validState().portfolios as any)[portfolioId];
  for (const field of Object.keys(canonical)) {
    const corrupted = { ...canonical };
    delete corrupted[field];
    const state = validState({ portfolios: { [portfolioId]: corrupted } });
    const path = stateFile(state);
    const decision = eliteAdmissionFromState(
      path, portfolioId, decisionAtMs, 20 * 60_000, undefined, admissionIndex(path),
    );
    assert.equal(decision.disposition, 'TRANSIENT', field);
    assert.equal(decision.retryable, true, field);
    assert.equal(shouldPersistAdmissionDenial(decision), false, field);
  }
  for (const bucket of ['ELITE_CANDIDATE', 'RESEARCH_WIDE'] as const) {
    const minimal = { portfolioId, observedAtMs: candidateObservedAtMs,
      selectorVersion: ELITE_SELECTOR_VERSION, bucket };
    const path = stateFile(validState({ portfolios: { [portfolioId]: minimal } }));
    const decision = eliteAdmissionFromState(
      path, portfolioId, decisionAtMs, 20 * 60_000, undefined, admissionIndex(path),
    );
    assert.equal(decision.disposition, 'TRANSIENT');
    assert.equal(shouldPersistAdmissionDenial(decision), false);
  }
});

test('candidate envelope version and future envelope fail transient before row selection', () => {
  for (const state of [
    { ...validState(), version: 2 },
    { ...validState(), version: undefined },
    { ...validState(), lastObservedAtMs: decisionAtMs + 1 },
  ]) {
    const path = stateFile(state);
    const decision = eliteAdmissionFromState(
      path, portfolioId, decisionAtMs, 20 * 60_000, undefined, admissionIndex(path),
    );
    assert.equal(decision.disposition, 'TRANSIENT');
    assert.equal(decision.retryable, true);
    assert.equal(shouldPersistAdmissionDenial(decision), false);
  }
});

test('feed-originated signal is admitted on causal elite qualification alone, with no admission index at all', () => {
  const path = stateFile(validState());
  const decision = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, undefined, undefined,
    undefined, decisionAtMs, false,
  );
  assert.equal(decision.allowed, true);
  assert.equal(decision.disposition, 'ALLOWED');
  assert.equal(decision.reason, 'elite_candidate_pretrade_qualified');
});

test('feed-originated signal ignores a missing, unhealthy, stale or corrupt direct-watch admission index', () => {
  const path = stateFile(validState());
  const missingIndexPath = join(path, '..', 'does-not-exist-admissions.json');
  const brokenIndexPath = join(path, '..', 'elite-direct-watch-admissions.json');

  const missing = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, undefined, missingIndexPath,
    undefined, decisionAtMs, false,
  );
  assert.equal(missing.allowed, true, 'a missing admission index must not gate a feed-originated signal');
  assert.equal(missing.reason, 'elite_candidate_pretrade_qualified');

  writeFileSync(brokenIndexPath, JSON.stringify({
    version: 2, generatedAtMs: decisionAtMs, healthy: false, suspensionReason: 'scan_in_progress', rows: {},
  }));
  const suspended = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, undefined, brokenIndexPath,
    undefined, decisionAtMs, false,
  );
  assert.equal(suspended.allowed, true, 'direct-watch suspension must not gate a feed-originated signal');
  assert.equal(suspended.reason, 'elite_candidate_pretrade_qualified');

  writeFileSync(brokenIndexPath, '{not valid json');
  const corrupt = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, undefined, brokenIndexPath,
    undefined, decisionAtMs, false,
  );
  assert.equal(corrupt.allowed, true, 'a corrupt admission index must not gate a feed-originated signal');
  assert.equal(corrupt.reason, 'elite_candidate_pretrade_qualified');

  writeFileSync(brokenIndexPath, JSON.stringify({
    version: 2, generatedAtMs: decisionAtMs, healthy: true, suspensionReason: null, rows: {},
  }));
  const emptyRows = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, undefined, brokenIndexPath,
    undefined, decisionAtMs, false,
  );
  assert.equal(emptyRows.allowed, true, 'no admitted interval for this portfolio must not gate a feed-originated signal');
});

test('feed-originated signal still fails closed on stale, future or non-elite candidate state', () => {
  const stale = eliteAdmissionFromState(
    stateFile(validState({ lastObservedAtMs: decisionAtMs - 21 * 60_000 })),
    portfolioId, decisionAtMs, 20 * 60_000, undefined, undefined, undefined, decisionAtMs, false,
  );
  assert.equal(stale.allowed, false);
  assert.equal(stale.reason, 'candidate_state_stale');

  const future = eliteAdmissionFromState(
    stateFile(validState({
      lastObservedAtMs: decisionAtMs - 10_000,
      firstEliteAtMs: { [portfolioId]: decisionAtMs + 1 },
    })),
    portfolioId, decisionAtMs, 20 * 60_000, undefined, undefined, undefined, decisionAtMs, false,
  );
  assert.equal(future.allowed, false);
  assert.equal(future.reason, 'portfolio_not_elite_at_decision_time');

  const notElite = eliteAdmissionFromState(
    stateFile(validState({
      portfolios: { [portfolioId]: { ...(validState().portfolios as any)[portfolioId], bucket: 'RESEARCH_WIDE' } },
    })),
    portfolioId, decisionAtMs, 20 * 60_000, undefined, undefined, undefined, decisionAtMs, false,
  );
  assert.equal(notElite.allowed, false);
  assert.equal(notElite.reason, 'portfolio_not_elite');
});

test('feed-originated signal resolves through the same historical pre-trade snapshot as direct-watch', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-feed-primary-historical-'));
  const path = join(dir, 'portfolio-candidates.json');
  const snapshots = join(dir, 'portfolio-candidate-snapshots.jsonl');
  writeFileSync(path, JSON.stringify(validState({ lastObservedAtMs: decisionAtMs + 20_000,
    portfolios: { [portfolioId]: {
      ...(validState().portfolios as any)[portfolioId], observedAtMs: decisionAtMs + 20_000,
      bucket: 'REJECTED_DEMOTED',
    } } })));
  writeFileSync(`${snapshots}.recent.json`, JSON.stringify({ version: 1,
    selectorVersion: ELITE_SELECTOR_VERSION, rows: [{
      ...(validState().portfolios as any)[portfolioId], observedAtMs: decisionAtMs - 1_000,
    }] }));
  const decision = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, snapshots, undefined,
    undefined, decisionAtMs + 30_000, false,
  );
  assert.equal(decision.allowed, true);
  assert.equal(decision.reason, 'elite_candidate_pretrade_snapshot_qualified');
});

test('direct-watch-originated signal (default requireDirectWatchAdmission) is unaffected by the feed-primary change', () => {
  const path = stateFile(validState());
  const waitlisted = eliteAdmissionFromState(path, portfolioId, decisionAtMs, 20 * 60_000, undefined, undefined, undefined, decisionAtMs, true);
  assert.equal(waitlisted.allowed, false);
  assert.equal(waitlisted.reason, 'direct_watch_admission_index_missing');

  const admitted = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, undefined, admissionIndex(path), undefined, decisionAtMs, true,
  );
  assert.equal(admitted.allowed, true);
  assert.equal(admitted.reason, 'elite_candidate_pretrade_qualified');
});

test('admission index requires exact complete writer row schema', () => {
  const valid = { portfolioId, admittedAtMs: candidateObservedAtMs - 1_000,
    admittedUntilMs: null, score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION };
  const { admittedUntilMs: _missingAdmittedUntilMs, ...missingAdmittedUntilMs } = valid;
  const corrupt = [
    missingAdmittedUntilMs,
    { ...valid, score: undefined }, { ...valid, score: '63.4' }, { ...valid, score: null },
    { ...valid, admittedAtMs: -1 }, { ...valid, admittedAtMs: '1' },
    { ...valid, selectorVersion: 'old' }, { ...valid, unexpectedCritical: {} },
  ];
  for (const row of corrupt) {
    const path = stateFile(validState());
    const index = join(path, '..', 'elite-direct-watch-admissions.json');
    writeFileSync(index, JSON.stringify({ version: 2, generatedAtMs: decisionAtMs,
      healthy: true, suspensionReason: null, rows: { [portfolioId]: row } }));
    const decision = eliteAdmissionFromState(path, portfolioId, decisionAtMs, 20 * 60_000, undefined, index);
    assert.equal(decision.reason, 'direct_watch_admission_index_invalid');
    assert.equal(decision.disposition, 'TRANSIENT');
    assert.equal(shouldPersistAdmissionDenial(decision), false);
  }
});
