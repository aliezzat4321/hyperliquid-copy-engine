import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { eliteAdmissionFromState } from '../src/elite-admission.js';

function stateFile(state: unknown) {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-elite-admission-'));
  const path = join(dir, 'portfolio-candidates.json');
  writeFileSync(path, JSON.stringify(state));
  return path;
}

const decisionAtMs = Date.UTC(2026, 8, 16, 12, 0, 0);
const candidateObservedAtMs = decisionAtMs - 60_000;
const portfolioId = 'elite-portfolio';

function validState(overrides: Record<string, unknown> = {}) {
  return {
    selectorVersion: 'invo-portfolio-elite-v1-20260916',
    lastObservedAtMs: candidateObservedAtMs,
    firstEliteAtMs: { [portfolioId]: candidateObservedAtMs - 1_000 },
    portfolios: {
      [portfolioId]: {
        portfolioId,
        observedAtMs: candidateObservedAtMs,
        bucket: 'ELITE_CANDIDATE',
        closedPositions: 140,
        winRatePct: 86,
        percentChange: 820,
        winLossRatio: 5.4,
        daysActive: 180,
        liquidated: false,
        sourceFilter: '1M',
        score: 512.2,
      },
    },
    ...overrides,
  };
}

test('admits only a portfolio proven elite before the decision', () => {
  const decision = eliteAdmissionFromState(stateFile(validState()), portfolioId, decisionAtMs, 20 * 60_000);
  assert.equal(decision.allowed, true);
  assert.equal(decision.reason, 'elite_candidate_pretrade_qualified');
  assert.equal(decision.portfolioId, portfolioId);
  assert.equal(decision.closedPositions, 140);
  assert.equal(decision.winRatePct, 86);
  assert.equal(decision.selectorVersion, 'invo-portfolio-elite-v1-20260916');
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

test('missing candidate state fails closed', () => {
  const decision = eliteAdmissionFromState('/does/not/exist/portfolio-candidates.json', portfolioId, decisionAtMs, 20 * 60_000);
  assert.equal(decision.allowed, false);
  assert.equal(decision.reason, 'candidate_state_missing');
});
