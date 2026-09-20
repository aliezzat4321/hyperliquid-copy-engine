import type { ManagedPosition } from './notification-state.js';
import { FUNDING_INTERVAL_MS } from './funding-oracle-capture.js';
import { FundingBoundaryStore } from './funding-boundary-store.js';

export interface FundingBoundarySyncResult {
  position: ManagedPosition;
  waited: boolean;
  appliedBoundaries: number[];
}

function activeSizeAt(position: ManagedPosition, boundaryMs: number): number {
  let size = 0;
  for (const checkpoint of [...(position.exposureCheckpoints ?? [])].sort((a, b) => a.atMs - b.atMs)) {
    if (checkpoint.atMs <= boundaryMs) size = Number(checkpoint.size);
    else break;
  }
  return Number.isFinite(size) ? size : 0;
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
    if (position.openedAtMs < boundary && activeSizeAt(position, boundary) > 0) boundaries.push(boundary);
  }
  return boundaries;
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
  let position = { ...original, fundingOracleCheckpoints: [...(original.fundingOracleCheckpoints ?? [])] };
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
    const result = record.result;
    const causallyValid = !result.failureClass && result.oraclePrices
      && result.finalObservedAtMs <= deadlineMs
      && result.finalDelayMs >= 0 && result.finalDelayMs <= maxDelayMs;
    if (!causallyValid) {
      position = { ...position, fundingIncompleteReason: `terminal oracle capture incomplete for funding interval ${boundary}` };
      break;
    }
    const oraclePx = Number(result.oraclePrices![position.coin]);
    if (!(oraclePx > 0)) {
      position = { ...position, fundingIncompleteReason: `missing oraclePx for ${position.coin} at funding interval ${boundary}` };
      break;
    }
    position.fundingOracleCheckpoints!.push({
      fundingTimeMs: boundary, observedAtMs: result.finalObservedAtMs, oraclePx,
    });
    appliedBoundaries.push(boundary);
  }
  return { position, waited, appliedBoundaries };
}
