import { existsSync, readFileSync, statSync } from 'fs';
import { ELITE_SELECTOR_VERSION } from './portfolio-candidates.js';

export const ELITE_ADMISSION_VERSION = 'lane3-elite-admission-v1-20260916';

interface SnapshotCacheEntry {
  mtimeMs: number;
  size: number;
  rows: any[];
}

const snapshotCache = new Map<string, SnapshotCacheEntry>();

const MAX_RECENT_INDEX_BYTES = 8 * 1024 * 1024;
const PORTFOLIO_BUCKETS = new Set([
  'ELITE_CANDIDATE', 'SPARSE_HIGH_RETURN', 'RESEARCH_WIDE', 'REJECTED_DEMOTED',
]);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value == null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function validRecentRow(row: unknown): boolean {
  if (!isPlainObject(row)) return false;
  const observedAtMs = row.observedAtMs;
  return typeof row.portfolioId === 'string' && row.portfolioId.trim().length > 0
    && row.selectorVersion === ELITE_SELECTOR_VERSION
    && typeof observedAtMs === 'number' && Number.isFinite(observedAtMs) && observedAtMs > 0
    && PORTFOLIO_BUCKETS.has(row.bucket as string);
}

function latestHistoricalSnapshot(
  snapshotsPath: string | undefined,
  portfolioId: string,
  decisionAtMs: number,
): { row: any | null; error: string | null } {
  if (!snapshotsPath) return { row: null, error: null };
  const indexPath = `${snapshotsPath}.recent.json`;
  if (!existsSync(indexPath)) return { row: null, error: null };
  let stat;
  try { stat = statSync(indexPath); } catch { return { row: null, error: 'candidate_snapshot_index_io_error' }; }
  if (stat.size > MAX_RECENT_INDEX_BYTES) return { row: null, error: 'candidate_snapshot_index_oversize' };
  let cached = snapshotCache.get(indexPath);
  if (!cached || cached.mtimeMs !== stat.mtimeMs || cached.size !== stat.size) {
    try {
      const parsed = JSON.parse(readFileSync(indexPath, 'utf8'));
      if (!isPlainObject(parsed) || parsed.version !== 1 || !Array.isArray(parsed.rows)) {
        return { row: null, error: 'candidate_snapshot_index_invalid_wrapper' };
      }
      if (!parsed.rows.every(validRecentRow)) {
        return { row: null, error: 'candidate_snapshot_index_invalid_row' };
      }
      cached = { mtimeMs: stat.mtimeMs, size: stat.size, rows: parsed.rows };
      snapshotCache.set(indexPath, cached);
    } catch { return { row: null, error: 'candidate_snapshot_index_unparseable' }; }
  }
  let chosen: any | null = null;
  for (const row of cached.rows) {
    if (row?.portfolioId !== portfolioId) continue;
    const observedAtMs = Number(row?.observedAtMs);
    if (Number.isFinite(observedAtMs) && observedAtMs <= decisionAtMs
      && (chosen == null || observedAtMs > Number(chosen.observedAtMs))) chosen = row;
  }
  return { row: chosen, error: null };
}

export interface EliteAdmissionDecision {
  allowed: boolean;
  disposition: 'ALLOWED' | 'TERMINAL' | 'TRANSIENT';
  retryable: boolean;
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
  directWatchAdmittedAtMs: number | null;
}

/** Only structural admission denials are terminal and safe to dedupe. */
export function shouldPersistAdmissionDenial(decision: EliteAdmissionDecision): boolean {
  return !decision.allowed && decision.disposition === 'TERMINAL' && !decision.retryable;
}

function finite(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function base(portfolioId: string): EliteAdmissionDecision {
  return {
    allowed: false,
    disposition: 'TERMINAL',
    retryable: false,
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
    directWatchAdmittedAtMs: null,
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
  snapshotsPath?: string,
  admissionIndexPath?: string,
  maxAdmissionIndexAgeMs = Math.min(maxStateAgeMs, 60_000),
): EliteAdmissionDecision {
  const denied = base(portfolioId);
  if (!portfolioId) return { ...denied, reason: 'portfolio_id_missing' };
  // Monitoring suspension is evaluated before candidate freshness. Otherwise a
  // temporary scan/cooldown can be mislabeled as a terminal stale-candidate denial
  // and ingress will consume the signal that the direct watcher is trying to protect.
  let admissionIndex: any = null;
  if (admissionIndexPath) {
    if (!existsSync(admissionIndexPath)) return { ...denied, disposition: 'TRANSIENT', retryable: true,
      reason: 'direct_watch_admission_index_missing' };
    try { admissionIndex = JSON.parse(readFileSync(admissionIndexPath, 'utf8')); }
    catch { return { ...denied, disposition: 'TRANSIENT', retryable: true,
      reason: 'direct_watch_admission_index_unparseable' }; }
    const indexGeneratedAtMs = finite(admissionIndex?.generatedAtMs);
    const indexRows = admissionIndex?.rows;
    const indexEnvelopeValid = admissionIndex != null
      && typeof admissionIndex === 'object'
      && !Array.isArray(admissionIndex)
      && admissionIndex.version === 1
      && typeof admissionIndex.healthy === 'boolean'
      && indexGeneratedAtMs != null
      && indexGeneratedAtMs > 0
      && indexRows != null
      && typeof indexRows === 'object'
      && !Array.isArray(indexRows)
      && (admissionIndex.suspensionReason === null
        || typeof admissionIndex.suspensionReason === 'string');
    if (!indexEnvelopeValid) {
      return { ...denied, disposition: 'TRANSIENT', retryable: true,
        reason: 'direct_watch_admission_index_invalid' };
    }
    if (indexGeneratedAtMs > decisionAtMs) {
      return { ...denied, disposition: 'TRANSIENT', retryable: true,
        reason: 'direct_watch_admission_index_from_future' };
    }
    if (decisionAtMs - indexGeneratedAtMs > maxAdmissionIndexAgeMs) {
      return { ...denied, disposition: 'TRANSIENT', retryable: true,
        reason: 'direct_watch_admission_index_stale' };
    }
    if (admissionIndex.healthy !== true) {
      const suspensionReason = typeof admissionIndex.suspensionReason === 'string'
        ? admissionIndex.suspensionReason : 'monitoring_unhealthy';
      return { ...denied, disposition: 'TRANSIENT', retryable: true,
        reason: `direct_watch_admission_suspended:${suspensionReason}` };
    }
    if (admissionIndex.suspensionReason !== null) {
      return { ...denied, disposition: 'TRANSIENT', retryable: true,
        reason: 'direct_watch_admission_index_invalid' };
    }
  }
  if (!existsSync(statePath)) return { ...denied, disposition: 'TRANSIENT', retryable: true,
    reason: 'candidate_state_missing' };

  let state: any;
  try {
    state = JSON.parse(readFileSync(statePath, 'utf8'));
  } catch {
    return { ...denied, disposition: 'TRANSIENT', retryable: true,
      reason: 'candidate_state_unparseable' };
  }
  if (!isPlainObject(state) || !isPlainObject(state.portfolios) || !isPlainObject(state.firstEliteAtMs)) {
    return { ...denied, disposition: 'TRANSIENT', retryable: true,
      reason: 'candidate_state_invalid_envelope' };
  }

  const selectorVersion = typeof state?.selectorVersion === 'string' ? state.selectorVersion : null;
  const stateObservedAtMs = finite(state?.lastObservedAtMs);
  const common = {
    ...denied,
    selectorVersion,
    candidateStateLastObservedAtMs: stateObservedAtMs,
  };

  if (!selectorVersion) return { ...common, disposition: 'TRANSIENT', retryable: true, reason: 'candidate_selector_version_missing' };
  if (selectorVersion !== ELITE_SELECTOR_VERSION) {
    return { ...common, disposition: 'TRANSIENT', retryable: true, reason: 'candidate_selector_version_mismatch' };
  }
  if (stateObservedAtMs == null || stateObservedAtMs <= 0) {
    return { ...common, disposition: 'TRANSIENT', retryable: true, reason: 'candidate_state_timestamp_missing' };
  }
  if (stateObservedAtMs <= decisionAtMs && decisionAtMs - stateObservedAtMs > maxStateAgeMs) {
    return { ...common, disposition: 'TRANSIENT', retryable: true, reason: 'candidate_state_stale' };
  }

  const historicalResult = latestHistoricalSnapshot(snapshotsPath, portfolioId, decisionAtMs);
  if (historicalResult.error) {
    return {
      ...common,
      disposition: 'TRANSIENT',
      retryable: true,
      reason: historicalResult.error,
    };
  }
  const historical = historicalResult.row;
  const current: any = (state.portfolios as Record<string, any>)[portfolioId];
  const candidate = historical ?? (
    current && finite(current?.observedAtMs) != null && Number(current.observedAtMs) <= decisionAtMs
      ? current
      : null
  );

  if (!candidate || typeof candidate !== 'object') {
    if (stateObservedAtMs > decisionAtMs) return { ...common, disposition: 'TRANSIENT', retryable: true, reason: 'candidate_state_from_future' };
    if (Object.prototype.hasOwnProperty.call(state.portfolios, portfolioId) && !isPlainObject(current)) {
      return { ...common, disposition: 'TRANSIENT', retryable: true, reason: 'candidate_state_invalid_candidate' };
    }
    if (current && finite(current?.observedAtMs) != null && Number(current.observedAtMs) > decisionAtMs) {
      return { ...common, disposition: 'TRANSIENT', retryable: true, reason: 'candidate_observation_from_future' };
    }
    return { ...common, reason: 'portfolio_not_in_candidate_state' };
  }

  const candidateSelectorVersion = typeof candidate?.selectorVersion === 'string'
    ? candidate.selectorVersion
    : selectorVersion;
  if (candidateSelectorVersion !== ELITE_SELECTOR_VERSION) {
    return { ...common, disposition: 'TRANSIENT', retryable: true, reason: 'candidate_selector_version_mismatch' };
  }

  const candidateObservedAtMs = finite(candidate.observedAtMs);
  const storedFirstEliteAtMs = finite(state?.firstEliteAtMs?.[portfolioId]);
  const firstEliteAtMs = storedFirstEliteAtMs ?? (
    candidate.bucket === 'ELITE_CANDIDATE' ? candidateObservedAtMs : null
  );
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

  if (candidateObservedAtMs == null || candidateObservedAtMs <= 0) {
    return { ...enriched, disposition: 'TRANSIENT', retryable: true, reason: 'candidate_observation_missing' };
  }
  if (candidateObservedAtMs > decisionAtMs) {
    return { ...enriched, disposition: 'TRANSIENT', retryable: true, reason: 'candidate_observation_from_future' };
  }
  if (decisionAtMs - candidateObservedAtMs > maxStateAgeMs) {
    return { ...enriched, disposition: 'TRANSIENT', retryable: true, reason: 'candidate_observation_stale' };
  }
  if (typeof candidate.bucket !== 'string' || !PORTFOLIO_BUCKETS.has(candidate.bucket)) {
    return { ...enriched, disposition: 'TRANSIENT', retryable: true, reason: 'candidate_bucket_invalid' };
  }
  if (candidate.bucket !== 'ELITE_CANDIDATE') {
    return { ...enriched, reason: 'portfolio_not_elite' };
  }
  if (firstEliteAtMs == null || firstEliteAtMs > decisionAtMs) {
    return { ...enriched, reason: 'portfolio_not_elite_at_decision_time' };
  }

  if (!admissionIndexPath || !existsSync(admissionIndexPath)) {
    return { ...enriched, disposition: 'TRANSIENT', retryable: true,
      reason: 'direct_watch_admission_index_missing' };
  }
  const hasAdmissionRow = Object.prototype.hasOwnProperty.call(admissionIndex.rows, portfolioId);
  if (!hasAdmissionRow) {
    return { ...enriched, reason: 'direct_watch_not_admitted' };
  }
  const admission = admissionIndex.rows[portfolioId];
  const admittedAtMs = finite(admission?.admittedAtMs);
  const indexGeneratedAtMs = finite(admissionIndex.generatedAtMs);
  const admissionRowValid = isPlainObject(admission)
    && admission.portfolioId === portfolioId
    && admission.selectorVersion === ELITE_SELECTOR_VERSION
    && admittedAtMs != null
    && admittedAtMs > 0
    && indexGeneratedAtMs != null
    && admittedAtMs <= indexGeneratedAtMs;
  if (!admissionRowValid) {
    return {
      ...enriched,
      disposition: 'TRANSIENT',
      retryable: true,
      reason: 'direct_watch_admission_index_invalid',
    };
  }
  if (admittedAtMs > decisionAtMs) {
    return { ...enriched, directWatchAdmittedAtMs: admittedAtMs, reason: 'direct_watch_not_admitted_at_signal_time' };
  }

  return {
    ...enriched,
    directWatchAdmittedAtMs: admittedAtMs,
    allowed: true,
    disposition: 'ALLOWED',
    retryable: false,
    reason: historical ? 'elite_candidate_pretrade_snapshot_qualified' : 'elite_candidate_pretrade_qualified',
  };
}
