import { existsSync, readFileSync } from 'fs';
import { ELITE_SELECTOR_VERSION } from './portfolio-candidates.js';

export const ELITE_ADMISSION_VERSION = 'lane3-elite-admission-v1-20260916';

export interface EliteAdmissionDecision {
  allowed: boolean;
  reason: string;
  admissionVersion: string;
  selectorVersion: string | null;
  portfolioId: string;
  candidateStateLastObservedAtMs: number | null;
  candidateObservedAtMs: number | null;
  firstEliteAtMs: number | null;
  bucket: string | null;
  closedPositions: number | null;
  closedPositionsPerDay: number | null;
  winRatePct: number | null;
  percentChange: number | null;
  winLossRatio: number | null;
  daysActive: number | null;
  recentActivityDaysAgo: number | null;
  liquidated: boolean | null;
  sourceFilter: string | null;
  score: number | null;
}

function finite(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function base(portfolioId: string): EliteAdmissionDecision {
  return {
    allowed: false,
    reason: 'candidate_state_unavailable',
    admissionVersion: ELITE_ADMISSION_VERSION,
    selectorVersion: null,
    portfolioId,
    candidateStateLastObservedAtMs: null,
    candidateObservedAtMs: null,
    firstEliteAtMs: null,
    bucket: null,
    closedPositions: null,
    closedPositionsPerDay: null,
    winRatePct: null,
    percentChange: null,
    winLossRatio: null,
    daysActive: null,
    recentActivityDaysAgo: null,
    liquidated: null,
    sourceFilter: null,
    score: null,
  };
}

/**
 * Fail-closed, decision-time portfolio admission for Lane 3 shadow opens/adds.
 * The portfolio research collector writes this state independently every 10 minutes.
 * No future observation, retroactive elite membership, or stale selector policy can
 * authorize an earlier signal.
 */
export function eliteAdmissionFromState(
  statePath: string,
  portfolioId: string,
  decisionAtMs: number,
  maxStateAgeMs: number,
): EliteAdmissionDecision {
  const denied = base(portfolioId);
  if (!portfolioId) return { ...denied, reason: 'portfolio_id_missing' };
  if (!existsSync(statePath)) return { ...denied, reason: 'candidate_state_missing' };

  let state: any;
  try {
    state = JSON.parse(readFileSync(statePath, 'utf8'));
  } catch {
    return { ...denied, reason: 'candidate_state_unparseable' };
  }

  const selectorVersion = typeof state?.selectorVersion === 'string' ? state.selectorVersion : null;
  const stateObservedAtMs = finite(state?.lastObservedAtMs);
  const common = {
    ...denied,
    selectorVersion,
    candidateStateLastObservedAtMs: stateObservedAtMs,
  };

  if (!selectorVersion) return { ...common, reason: 'candidate_selector_version_missing' };
  if (selectorVersion !== ELITE_SELECTOR_VERSION) {
    return { ...common, reason: 'candidate_selector_version_mismatch' };
  }
  if (stateObservedAtMs == null || stateObservedAtMs <= 0) return { ...common, reason: 'candidate_state_timestamp_missing' };
  if (stateObservedAtMs > decisionAtMs) return { ...common, reason: 'candidate_state_from_future' };
  if (decisionAtMs - stateObservedAtMs > maxStateAgeMs) return { ...common, reason: 'candidate_state_stale' };

  const candidate = state?.portfolios?.[portfolioId];
  if (!candidate || typeof candidate !== 'object') return { ...common, reason: 'portfolio_not_in_candidate_state' };

  const candidateObservedAtMs = finite(candidate.observedAtMs);
  const firstEliteAtMs = finite(state?.firstEliteAtMs?.[portfolioId]);
  const enriched: EliteAdmissionDecision = {
    ...common,
    candidateObservedAtMs,
    firstEliteAtMs,
    bucket: typeof candidate.bucket === 'string' ? candidate.bucket : null,
    closedPositions: finite(candidate.closedPositions),
    closedPositionsPerDay: finite(candidate.closedPositionsPerDay),
    winRatePct: finite(candidate.winRatePct),
    percentChange: finite(candidate.percentChange),
    winLossRatio: finite(candidate.winLossRatio),
    daysActive: finite(candidate.daysActive),
    recentActivityDaysAgo: finite(candidate.recentActivityDaysAgo),
    liquidated: typeof candidate.liquidated === 'boolean' ? candidate.liquidated : null,
    sourceFilter: typeof candidate.sourceFilter === 'string' ? candidate.sourceFilter : null,
    score: finite(candidate.score),
  };

  if (candidateObservedAtMs == null || candidateObservedAtMs <= 0) return { ...enriched, reason: 'candidate_observation_missing' };
  if (candidateObservedAtMs > decisionAtMs) return { ...enriched, reason: 'candidate_observation_from_future' };
  if (decisionAtMs - candidateObservedAtMs > maxStateAgeMs) return { ...enriched, reason: 'candidate_observation_stale' };
  if (candidate.bucket !== 'ELITE_CANDIDATE') return { ...enriched, reason: 'portfolio_not_elite' };
  if (firstEliteAtMs == null || firstEliteAtMs > decisionAtMs) return { ...enriched, reason: 'portfolio_not_elite_at_decision_time' };

  return { ...enriched, allowed: true, reason: 'elite_candidate_pretrade_qualified' };
}
