import type { ManagedPosition } from './notification-state.js';
import { FUNDING_INTERVAL_MS, type FundingOracleCaptureResult } from './funding-oracle-capture.js';
import { FundingBoundaryStore } from './funding-boundary-store.js';

export interface FundingBoundarySyncResult {
  position: ManagedPosition;
  waited: boolean;
  appliedBoundaries: number[];
}

export function boundaryExposure(position: ManagedPosition, boundaryMs: number): number {
  if (!(position.openedAtMs < boundaryMs)) return 0;
  let size = 0;
  for (const checkpoint of [...(position.exposureCheckpoints ?? [])].sort((a, b) => a.atMs - b.atMs)) {
    if (checkpoint.atMs <= boundaryMs) size = Number(checkpoint.size);
    else break;
  }
  return Number.isFinite(size) ? size : 0;
}

export function isPositionExposedAcrossBoundary(position: ManagedPosition, boundaryMs: number): boolean {
  return boundaryExposure(position, boundaryMs) > 0;
}

export interface FundingBoundaryApplication {
  position: ManagedPosition;
  applied: boolean;
  incomplete: boolean;
}

/** Canonical terminal-result application shared by worker delivery and close sync. */
export function applyFundingOracleResultToPosition(
  original: ManagedPosition,
  result: FundingOracleCaptureResult,
  maxDelayMs: number,
): FundingBoundaryApplication {
  const boundary = result.fundingTimeMs;
  if (original.fundingIncompleteReason
      || (original.fundingOracleCheckpoints ?? []).some(point => point.fundingTimeMs === boundary)
      || !isPositionExposedAcrossBoundary(original, boundary)) {
    return { position: original, applied: false, incomplete: false };
  }
  const deadlineMs = boundary + maxDelayMs;
  const causallyValid = !result.failureClass && result.oraclePrices
    && result.finalObservedAtMs <= deadlineMs
    && result.finalDelayMs >= 0 && result.finalDelayMs <= maxDelayMs;
  if (!causallyValid) {
    return {
      position: { ...original, fundingIncompleteReason: `terminal oracle capture incomplete for funding interval ${boundary}` },
      applied: false,
      incomplete: true,
    };
  }
  const oraclePx = Number(result.oraclePrices![original.coin]);
  if (!(oraclePx > 0)) {
    return {
      position: { ...original, fundingIncompleteReason: `missing oraclePx for ${original.coin} at funding interval ${boundary}` },
      applied: false,
      incomplete: true,
    };
  }
  return {
    position: {
      ...original,
      fundingOracleCheckpoints: [
        ...(original.fundingOracleCheckpoints ?? []),
        { fundingTimeMs: boundary, observedAtMs: result.finalObservedAtMs, oraclePx },
      ],
    },
    applied: true,
    incomplete: false,
  };
}

export function crossedFundingBoundaries(
  position: ManagedPosition,
  closedAtMs: number,
  intervalMs = FUNDING_INTERVAL_MS,
): number[] {
  if (!(closedAtMs > position.openedAtMs) || !(intervalMs > 0)) return [];
  const accruedThroughMs = Number(position.fundingAccruedThroughMs ?? position.openedAtMs);
  const first = Math.floor(Math.max(position.openedAtMs, accruedThroughMs) / intervalMs) * intervalMs + intervalMs;
  const boundaries: number[] = [];
  for (let boundary = first; boundary <= closedAtMs; boundary += intervalMs) {
    // Strictly-open-before plus the exposure ledger prevents applying evidence to a
    // position opened after, or already reduced to zero before, this boundary.
    if (isPositionExposedAcrossBoundary(position, boundary)) boundaries.push(boundary);
  }
  return boundaries;
}

export function firstUnappliedFundingBoundary(
  position: ManagedPosition,
  intervalMs = FUNDING_INTERVAL_MS,
): number {
  const accruedThroughMs = Number(position.fundingAccruedThroughMs ?? position.openedAtMs);
  let boundary = Math.floor(Math.max(position.openedAtMs, accruedThroughMs) / intervalMs) * intervalMs + intervalMs;
  const applied = new Set((position.fundingOracleCheckpoints ?? []).map(point => point.fundingTimeMs));
  while (applied.has(boundary)) boundary += intervalMs;
  return boundary;
}

export async function syncStagedFundingForClose(
  original: ManagedPosition,
  closedAtMs: number,
  store: FundingBoundaryStore,
  maxDelayMs: number,
  options: {
    intervalMs?: number;
    now?: () => number;
    sleep?: (delayMs: number) => Promise<void>;
    pollMs?: number;
  } = {},
): Promise<FundingBoundarySyncResult> {
  const now = options.now ?? Date.now;
  const sleep = options.sleep ?? (delayMs => new Promise(resolve => setTimeout(resolve, delayMs)));
  const pollMs = options.pollMs ?? 25;
  let position: ManagedPosition = {
    ...original,
    fundingOracleCheckpoints: [...(original.fundingOracleCheckpoints ?? [])],
  };
  let waited = false;
  const appliedBoundaries: number[] = [];
  for (const boundary of crossedFundingBoundaries(position, closedAtMs, options.intervalMs)) {
    if (position.fundingIncompleteReason) break;
    if (position.fundingOracleCheckpoints!.some(point => point.fundingTimeMs === boundary)) continue;
    const deadlineMs = boundary + maxDelayMs;
    let record = store.read(boundary);
    while (!record && now() <= deadlineMs) {
      waited = true;
      await sleep(Math.max(1, Math.min(pollMs, deadlineMs - now() + 1)));
      record = store.read(boundary);
    }
    if (!record) {
      position = { ...position, fundingIncompleteReason: `missed durable oracle checkpoint for funding interval ${boundary}` };
      break;
    }
    const application = applyFundingOracleResultToPosition(position, record.result, maxDelayMs);
    position = application.position;
    if (application.applied) appliedBoundaries.push(boundary);
    if (application.incomplete) break;
  }
  return { position, waited, appliedBoundaries };
}
