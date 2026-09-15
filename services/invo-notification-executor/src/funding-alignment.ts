import type { ExposureCheckpoint, FundingOracleCheckpoint, FundingPoint } from './shadow-execution.js';

export const FUNDING_BOUNDARY_TOLERANCE_MS = 1_000;

function activeExposureAt(checkpoints: ExposureCheckpoint[], atMs: number): ExposureCheckpoint | null {
  let active: ExposureCheckpoint | null = null;
  for (const checkpoint of checkpoints) {
    if (checkpoint.atMs <= atMs) active = checkpoint;
    else break;
  }
  return active;
}

export function alignFundingHistoryToCapturedBoundaries(
  checkpoints: ExposureCheckpoint[],
  history: FundingPoint[],
  oracleByFundingTime: Map<number, FundingOracleCheckpoint>,
  toleranceMs = FUNDING_BOUNDARY_TOLERANCE_MS,
): Map<number, FundingPoint> {
  if (!Number.isFinite(toleranceMs) || toleranceMs < 0) {
    throw new Error(`Invalid funding boundary tolerance: ${toleranceMs}`);
  }

  const boundaries = [...oracleByFundingTime.keys()].sort((a, b) => a - b);
  const aligned = new Map<number, FundingPoint>();

  for (const point of history) {
    const candidates = boundaries.filter(boundary => Math.abs(point.timeMs - boundary) <= toleranceMs);
    if (candidates.length > 1) {
      throw new Error(`Ambiguous funding-history boundary alignment for row ${point.timeMs}`);
    }
    if (candidates.length === 0) {
      const active = activeExposureAt(checkpoints, point.timeMs);
      if (active && active.size > 0) {
        throw new Error(`Missing fresh oracle checkpoint for funding interval ${point.timeMs}`);
      }
      continue;
    }

    const boundary = candidates[0];
    if (aligned.has(boundary)) {
      throw new Error(`Duplicate funding-history rows for captured oracle interval ${boundary}`);
    }
    aligned.set(boundary, { timeMs: boundary, rate: point.rate });
  }

  for (const boundary of boundaries) {
    const active = activeExposureAt(checkpoints, boundary);
    if (active && active.size > 0 && !aligned.has(boundary)) {
      throw new Error(`Missing funding-history row for captured oracle interval ${boundary}`);
    }
  }

  return aligned;
}
