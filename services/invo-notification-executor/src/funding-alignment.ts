import type { FundingPoint } from './shadow-execution.js';

export const FUNDING_BOUNDARY_TOLERANCE_MS = 1_000;

export function alignFundingHistoryToCapturedBoundaries(
  history: FundingPoint[],
  capturedFundingTimes: number[],
  toleranceMs = FUNDING_BOUNDARY_TOLERANCE_MS,
): FundingPoint[] {
  if (!Number.isFinite(toleranceMs) || toleranceMs < 0) {
    throw new Error(`Invalid funding boundary tolerance: ${toleranceMs}`);
  }

  const boundaries = [...new Set(capturedFundingTimes.filter(Number.isFinite))].sort((a, b) => a - b);
  const seenBoundaries = new Set<number>();

  return history.map(point => {
    const candidates = boundaries.filter(boundary => Math.abs(point.timeMs - boundary) <= toleranceMs);
    if (candidates.length > 1) {
      throw new Error(`Ambiguous funding-history boundary alignment for row ${point.timeMs}`);
    }
    if (candidates.length === 0) return point;

    const boundary = candidates[0];
    if (seenBoundaries.has(boundary)) {
      throw new Error(`Duplicate funding-history rows for captured oracle interval ${boundary}`);
    }
    seenBoundaries.add(boundary);
    return { timeMs: boundary, rate: point.rate };
  });
}
