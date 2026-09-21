import assert from 'node:assert/strict';
import { mkdtempSync, truncateSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { eliteAdmissionFromState, shouldPersistAdmissionDenial } from '../src/elite-admission.js';
import { ELITE_SELECTOR_VERSION } from '../src/portfolio-candidates.js';

function stateFile(state: unknown) {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-elite-admission-'));
  const path = join(dir, 'portfolio-candidates.json');
  writeFileSync(path, JSON.stringify(state));
  return path;
}

function admissionIndex(statePath: string, admittedAtMs = candidateObservedAtMs - 1_000) {
  const path = join(statePath, '..', 'elite-direct-watch-admissions.json');
  writeFileSync(path, JSON.stringify({ version: 1, generatedAtMs: decisionAtMs, rows: {
    [portfolioId]: { portfolioId, admittedAtMs, score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
  } }));
  return path;
}

const decisionAtMs = Date.UTC(2026, 8, 16, 12, 0, 0);
const candidateObservedAtMs = decisionAtMs - 60_000;
const portfolioId = 'elite-portfolio';

function validState(overrides: Record<string, unknown> = {}) {
  return {
    selectorVersion: ELITE_SELECTOR_VERSION,
    lastObservedAtMs: candidateObservedAtMs,
    firstEliteAtMs: { [portfolioId]: candidateObservedAtMs - 1_000 },
    portfolios: {
      [portfolioId]: {
        portfolioId,
        observedAtMs: candidateObservedAtMs,
        bucket: 'ELITE_CANDIDATE',
        closedPositions: 24,
        closedPositionsPerDay: 3,
        winRatePct: 83.3,
        percentChange: 120,
        winLossRatio: 4.99,
        daysActive: 8,
        recentActivityDaysAgo: 0.25,
        liquidated: false,
        sourceFilter: '1W',
        score: 63.4,
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

test('qualified candidate is waitlisted until direct-watch admission and cannot replay pre-admission feed events', () => {
  const path = stateFile(validState());
  const waitlisted = eliteAdmissionFromState(path, portfolioId, decisionAtMs, 20 * 60_000);
  assert.equal(waitlisted.allowed, false);
  assert.equal(waitlisted.reason, 'direct_watch_admission_index_missing');

  const index = admissionIndex(path, decisionAtMs + 10);
  const preAdmission = eliteAdmissionFromState(path, portfolioId, decisionAtMs, 20 * 60_000, undefined, index);
  assert.equal(preAdmission.allowed, false);
  assert.equal(preAdmission.reason, 'direct_watch_not_admitted_at_signal_time');
  const prospective = eliteAdmissionFromState(path, portfolioId, decisionAtMs + 11, 20 * 60_000, undefined, index);
  assert.equal(prospective.allowed, true);
  assert.equal(prospective.directWatchAdmittedAtMs, decisionAtMs + 10);
});

test('feed NEW during scan suspension remains unseen and cursor-safe, then executes once after health returns', () => {
  const path = stateFile(validState());
  const index = admissionIndex(path);
  writeFileSync(index, JSON.stringify({ version: 1, generatedAtMs: decisionAtMs,
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

  writeFileSync(index, JSON.stringify({ version: 1, generatedAtMs: decisionAtMs + 1,
    healthy: true, suspensionReason: null, rows: {
      [portfolioId]: { portfolioId, admittedAtMs: candidateObservedAtMs - 1_000,
        score: 63.4, selectorVersion: ELITE_SELECTOR_VERSION },
    } }));
  const recovered = eliteAdmissionFromState(
    path, portfolioId, decisionAtMs, 20 * 60_000, undefined, index,
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

test('historical snapshot authorizes a trade when latest aggregate state is newer than the trade', () => {
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
  writeFileSync(`${snapshotsPath}.recent.json`, JSON.stringify({ version: 1, rows: [{
    ...(validState().portfolios as any)[portfolioId],
    selectorVersion: ELITE_SELECTOR_VERSION,
    observedAtMs: historicalAt,
    bucket: 'ELITE_CANDIDATE',
  }] }));
  const decision = eliteAdmissionFromState(
    statePath, portfolioId, decisionAtMs, 20 * 60_000, snapshotsPath, admissionIndex(statePath),
  );
  assert.equal(decision.allowed, true);
  assert.equal(decision.reason, 'elite_candidate_pretrade_snapshot_qualified');
  assert.equal(decision.candidateObservedAtMs, historicalAt);
});

test('latest pre-trade snapshot preserves demotion and prevents look-ahead re-promotion', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-elite-history-demotion-'));
  const statePath = join(dir, 'portfolio-candidates.json');
  const snapshotsPath = join(dir, 'portfolio-candidate-snapshots.jsonl');
  const eliteAt = decisionAtMs - 10 * 60_000;
  const demotedAt = decisionAtMs - 60_000;
  const futureAt = decisionAtMs + 60_000;
  writeFileSync(statePath, JSON.stringify(validState({
    lastObservedAtMs: futureAt,
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
  writeFileSync(`${snapshotsPath}.recent.json`, JSON.stringify({ version: 1, rows: [
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
  writeFileSync(`${snapshotsPath}.recent.json`, JSON.stringify({ version: 1, rows: [
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
  const futureState = validState({ lastObservedAtMs: decisionAtMs + 1 });
  writeFileSync(statePath, JSON.stringify(futureState));
  writeFileSync(`${snapshotsPath}.recent.json`, '{"version":1,"rows":[');
  const decision = eliteAdmissionFromState(statePath, portfolioId, decisionAtMs, 20 * 60_000, snapshotsPath);
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, 'candidate_snapshot_index_unparseable');
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
    writeFileSync(`${snapshotsPath}.recent.json`, JSON.stringify({ version: 1, rows }));
    const decision = eliteAdmissionFromState(
      statePath, portfolioId, decisionAtMs, 20 * 60_000, snapshotsPath,
    );
    assert.equal(decision.allowed, false, `case ${index} must fail closed`);
    assert.equal(decision.reason, 'candidate_snapshot_index_invalid_row');
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
  }
});
