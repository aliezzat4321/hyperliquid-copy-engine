import * as hl from './hl-client.js';
import type { ManagedPosition } from './notification-state.js';
import {
  closeAction,
  computePositionEconomics,
  fundingCostUsd,
  normalizeL2Book,
  simulateL2Fill,
  type BookAction,
  type FundingPoint,
  type ShadowExecutionPolicy,
} from './shadow-execution.js';

export const EXECUTION_EVIDENCE_VERSION = 'lane3-causal-l2-v2';
export const COST_MODEL_VERSION = 'hl-taker-l2-oracle-funding-v2';
export const FUNDING_MODEL_VERSION = 'funding-history-rate-x-prospective-oracle-x-size-v2';
const META_TTL_MS = 5 * 60 * 1000;

type Meta = Awaited<ReturnType<typeof hl.getMeta>>;
let metaCache: { value: Meta; expiresAtMs: number } | null = null;
let metaRequest: Promise<Meta> | null = null;

async function getCachedMeta(nowMs = Date.now()): Promise<Meta> {
  if (metaCache && metaCache.expiresAtMs > nowMs) return metaCache.value;
  if (!metaRequest) {
    metaRequest = hl.getMeta().then(value => {
      metaCache = { value, expiresAtMs: Date.now() + META_TTL_MS };
      return value;
    }).finally(() => { metaRequest = null; });
  }
  return metaRequest;
}

export function resetMetaCacheForTest(): void {
  metaCache = null;
  metaRequest = null;
}

export interface AssetBook {
  assetIndex: number;
  asset: { name: string; szDecimals: number; maxLeverage: number };
  requestedAtMs: number;
  receivedAtMs: number;
  book: ReturnType<typeof normalizeL2Book>;
}

export async function fetchAssetBook(coin: string): Promise<AssetBook | null> {
  const requestedAtMs = Date.now();
  const bookRequest = hl.getL2Book(coin).then(rawBook => ({ rawBook, receivedAtMs: Date.now() }));
  const [meta, bookResponse] = await Promise.all([getCachedMeta(requestedAtMs), bookRequest]);
  const { rawBook, receivedAtMs } = bookResponse;
  const assetIndex = meta.universe.findIndex((asset: any) => asset.name === coin);
  if (assetIndex < 0) return null;
  const asset = meta.universe[assetIndex];
  return {
    assetIndex,
    asset,
    requestedAtMs,
    receivedAtMs,
    book: normalizeL2Book(rawBook, requestedAtMs, receivedAtMs, coin),
  };
}

export async function executableShadowFill(
  coin: string,
  action: BookAction,
  rawSize: number,
  policy: ShadowExecutionPolicy,
): Promise<{
  assetBook: AssetBook | null;
  result: ReturnType<typeof simulateL2Fill>;
}> {
  const assetBook = await fetchAssetBook(coin);
  if (!assetBook) return { assetBook: null, result: { ok: false, reason: 'missing_book' } };
  return {
    assetBook,
    result: simulateL2Fill(
      assetBook.book,
      action,
      rawSize,
      assetBook.asset.szDecimals,
      policy,
    ),
  };
}

export async function fundingForPosition(
  position: ManagedPosition,
  endTimeMs: number,
  maxOracleDelayMs = 10_000,
  evidence?: (record: FundingHistoryEvidence) => void,
): Promise<{
  fundingUsd: number;
  fundingPoints: number;
  oraclePointsMatched: number;
  carryUsd: number;
  model: string;
}> {
  if (position.fundingIncompleteReason) {
    throw new Error(`funding evidence already incomplete: ${position.fundingIncompleteReason}`);
  }
  const checkpoints = position.exposureCheckpoints ?? [];
  if (!checkpoints.length) {
    throw new Error('position has no size checkpoints for funding accounting');
  }
  if (!Array.isArray(position.fundingOracleCheckpoints)) {
    throw new Error('position has no prospective oracle checkpoint ledger');
  }
  const carryUsd = Number(position.fundingCarryUsd ?? 0);
  if (!Number.isFinite(carryUsd)) throw new Error('invalid funding carry');
  const accruedThroughMs = Number(position.fundingAccruedThroughMs ?? position.openedAtMs);
  // Query on the exact first captured settlement boundary. Using the arbitrary open-fill
  // millisecond as startTime made the API boundary contract implicit and made partial-close
  // +1 arithmetic capable of hiding the row whose oracle checkpoint proves it is required.
  const expectedBoundaryTimes = position.fundingOracleCheckpoints
    .map(point => point.fundingTimeMs)
    .filter(timeMs => timeMs > accruedThroughMs && timeMs <= endTimeMs)
    .sort((a, b) => a - b);
  const startTimeMs = expectedBoundaryTimes[0] ?? endTimeMs + 1;
  let raw: Awaited<ReturnType<typeof hl.getFundingHistory>> = [];
  try {
    if (expectedBoundaryTimes.length) {
      raw = await hl.getFundingHistory(position.coin, startTimeMs, endTimeMs);
    }
  } catch (cause) {
    throw new FundingEvidenceError(cause instanceof Error ? cause.message : String(cause), {
      coin: position.coin,
      queriedStartTimeMs: startTimeMs,
      queriedEndTimeMs: endTimeMs,
      returnedRowTimesMs: [],
      rawRows: [],
    });
  }
  const record: FundingHistoryEvidence = {
    coin: position.coin,
    queriedStartTimeMs: startTimeMs,
    queriedEndTimeMs: endTimeMs,
    returnedRowTimesMs: raw.map(row => Number(row.time)).filter(Number.isFinite),
    rawRows: raw,
  };
  evidence?.(record);
  const history: FundingPoint[] = raw
    .map(row => ({ timeMs: Number(row.time), rate: Number(row.fundingRate) }))
    .filter(row => Number.isFinite(row.timeMs) && Number.isFinite(row.rate));
  let calculated;
  try {
    calculated = fundingCostUsd(
      position.side,
      checkpoints,
      history,
      position.fundingOracleCheckpoints.filter(point => point.fundingTimeMs > accruedThroughMs),
      maxOracleDelayMs,
    );
  } catch (cause) {
    throw new FundingEvidenceError(cause instanceof Error ? cause.message : String(cause), record);
  }
  return {
    fundingUsd: carryUsd + calculated.fundingUsd,
    fundingPoints: calculated.fundingPoints,
    oraclePointsMatched: calculated.oraclePointsMatched,
    carryUsd,
    model: FUNDING_MODEL_VERSION,
  };
}

export interface FundingHistoryEvidence {
  coin: string;
  queriedStartTimeMs: number;
  queriedEndTimeMs: number;
  returnedRowTimesMs: number[];
  rawRows: Awaited<ReturnType<typeof hl.getFundingHistory>>;
}

export class FundingEvidenceError extends Error {
  constructor(message: string, readonly evidence: FundingHistoryEvidence) {
    super(message);
    this.name = 'FundingEvidenceError';
  }
}

export interface ShadowMark {
  sourceBaseId: string;
  coin: string;
  side: ManagedPosition['side'];
  size: number | null;
  status:
    | 'MARKED'
    | 'PARTIAL_DEPTH'
    | 'INCOMPLETE_LEGACY_ENTRY'
    | 'BOOK_REJECTED'
    | 'FUNDING_UNAVAILABLE';
  reason?: string;
  entryPrice?: number;
  markExitPrice?: number;
  markedSize?: number;
  unfilledSize?: number;
  grossPnlUsd?: number;
  netPnlUsd?: number;
  grossReturnBps?: number;
  netReturnBps?: number;
  fundingUsd?: number;
  entryFeeUsd?: number;
  exitFeeUsd?: number;
  spreadBps?: number;
  slippageBps?: number;
  bookAgeMs?: number;
  bookTimeMs?: number;
  markedAtMs: number;
  executionEvidenceVersion?: string;
  costModelVersion?: string;
}

export async function markShadowPosition(
  position: ManagedPosition,
  policy: ShadowExecutionPolicy,
): Promise<ShadowMark> {
  const markedAtMs = Date.now();
  const size = Number(position.size);
  if (
    position.executionEvidenceVersion !== EXECUTION_EVIDENCE_VERSION
    || !(Number(position.entryPrice) > 0)
    || !(size > 0)
    || !(Number(position.entryNotionalExecutedUsd) > 0)
    || !Array.isArray(position.exposureCheckpoints)
    || !Array.isArray(position.fundingOracleCheckpoints)
  ) {
    return {
      sourceBaseId: position.sourceBaseId,
      coin: position.coin,
      side: position.side,
      size: Number.isFinite(size) ? size : null,
      status: 'INCOMPLETE_LEGACY_ENTRY',
      reason: 'missing v2 causal entry/funding provenance',
      markedAtMs,
      executionEvidenceVersion: position.executionEvidenceVersion,
      costModelVersion: position.costModelVersion,
    };
  }

  const { result } = await executableShadowFill(
    position.coin,
    closeAction(position.side),
    size,
    policy,
  );
  if (!result.ok) {
    return {
      sourceBaseId: position.sourceBaseId,
      coin: position.coin,
      side: position.side,
      size,
      status: 'BOOK_REJECTED',
      reason: result.reason,
      markedAtMs,
      executionEvidenceVersion: position.executionEvidenceVersion,
      costModelVersion: position.costModelVersion,
    };
  }

  const fill = result.fill;
  const fraction = Math.min(1, fill.filledSize / size);
  let funding;
  try {
    funding = await fundingForPosition(position, markedAtMs, policy.fundingOracleMaxDelayMs ?? 10_000);
  } catch (err) {
    return {
      sourceBaseId: position.sourceBaseId,
      coin: position.coin,
      side: position.side,
      size,
      status: 'FUNDING_UNAVAILABLE',
      reason: err instanceof Error ? err.message : String(err),
      entryPrice: position.entryPrice,
      markExitPrice: fill.avgPx,
      markedSize: fill.filledSize,
      unfilledSize: fill.unfilledSize,
      spreadBps: fill.spreadBps,
      slippageBps: fill.slippageBps,
      bookAgeMs: fill.bookAgeMs,
      bookTimeMs: fill.bookTimeMs,
      markedAtMs,
      executionEvidenceVersion: position.executionEvidenceVersion,
      costModelVersion: position.costModelVersion,
    };
  }

  const economics = computePositionEconomics({
    side: position.side,
    entryAvgPx: Number(position.entryPrice),
    size: fill.filledSize,
    entryFeeUsd: Number(position.entryFeeUsd ?? 0) * fraction,
    entryNotionalUsd: Number(position.entryNotionalExecutedUsd) * fraction,
    exitFill: fill,
    fundingUsd: funding.fundingUsd * fraction,
  });
  return {
    sourceBaseId: position.sourceBaseId,
    coin: position.coin,
    side: position.side,
    size,
    status: fill.partial ? 'PARTIAL_DEPTH' : 'MARKED',
    entryPrice: position.entryPrice,
    markExitPrice: fill.avgPx,
    markedSize: fill.filledSize,
    unfilledSize: fill.unfilledSize,
    grossPnlUsd: economics.grossPnlUsd,
    netPnlUsd: economics.netPnlUsd,
    grossReturnBps: economics.grossReturnBps,
    netReturnBps: economics.netReturnBps,
    fundingUsd: economics.fundingUsd,
    entryFeeUsd: economics.entryFeeUsd,
    exitFeeUsd: economics.exitFeeUsd,
    spreadBps: fill.spreadBps,
    slippageBps: fill.slippageBps,
    bookAgeMs: fill.bookAgeMs,
    bookTimeMs: fill.bookTimeMs,
    markedAtMs,
    executionEvidenceVersion: position.executionEvidenceVersion,
    costModelVersion: position.costModelVersion,
  };
}
