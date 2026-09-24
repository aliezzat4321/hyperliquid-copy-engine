import * as hl from './hl-client.js';
import type { ManagedPosition } from './notification-state.js';
import { alignFundingHistoryToCapturedBoundaries } from './funding-alignment.js';
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
const META_CACHE_TTL_MS = 60_000;
let metaCache: { value: Awaited<ReturnType<typeof hl.getMeta>>; expiresAtMs: number } | null = null;
let metaInFlight: Promise<Awaited<ReturnType<typeof hl.getMeta>>> | null = null;

async function getCachedMeta(nowMs = Date.now()) {
  if (metaCache && nowMs < metaCache.expiresAtMs) return metaCache.value;
  if (!metaInFlight) {
    metaInFlight = hl.getMeta().then(value => {
      metaCache = { value, expiresAtMs: Date.now() + META_CACHE_TTL_MS };
      return value;
    }).finally(() => { metaInFlight = null; });
  }
  return metaInFlight;
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
  const metaPromise = getCachedMeta(requestedAtMs);
  const rawBook = await hl.getL2Book(coin);
  // Book receipt is the completion of the l2Book request. Metadata latency must never
  // make an otherwise fresh book appear stale.
  const receivedAtMs = Date.now();
  const returnedCoin = String(rawBook?.coin ?? '').trim();
  if (!returnedCoin) {
    throw new Error(`L2 book coin missing: requested ${coin}`);
  }
  if (returnedCoin.toUpperCase() !== coin.trim().toUpperCase()) {
    throw new Error(`L2 book coin mismatch: requested ${coin}, received ${returnedCoin}`);
  }
  const meta = await metaPromise;
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
  onPage?: () => void,
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
  const startTimeMs = accruedThroughMs > position.openedAtMs ? accruedThroughMs + 1 : position.openedAtMs;
  const query = await hl.getFundingHistory(position.coin, startTimeMs, endTimeMs, onPage);
  const rawHistory: FundingPoint[] = query.rows
    .map(row => ({ timeMs: Number(row.time), rate: Number(row.fundingRate) }))
    .filter(row => Number.isFinite(row.timeMs) && Number.isFinite(row.rate));
  const history = alignFundingHistoryToCapturedBoundaries(
    rawHistory,
    position.fundingOracleCheckpoints.map(checkpoint => checkpoint.fundingTimeMs),
  );
  let calculated;
  try {
    calculated = fundingCostUsd(
      position.side, checkpoints, history, position.fundingOracleCheckpoints, maxOracleDelayMs,
    );
  } catch (err) {
    throw new Error(`${err instanceof Error ? err.message : String(err)}; fundingQuery=${JSON.stringify(query.diagnostics)}`);
  }
  return {
    fundingUsd: carryUsd + calculated.fundingUsd,
    fundingPoints: calculated.fundingPoints,
    oraclePointsMatched: calculated.oraclePointsMatched,
    carryUsd,
    model: FUNDING_MODEL_VERSION,
  };
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

function markRecord(value: unknown, path: string): Record<string, unknown> {
  if (value == null || typeof value !== 'object' || Array.isArray(value)
    || ![Object.prototype, null].includes(Object.getPrototypeOf(value))) {
    throw new Error(`${path} must be a plain object`);
  }
  return value as Record<string, unknown>;
}
function markString(value: unknown, path: string) {
  if (typeof value !== 'string' || value.length === 0) throw new Error(`${path} must be a non-empty string`);
}
function markNumber(value: unknown, path: string, nullable = false) {
  if (nullable && value === null) return;
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`${path} must be a finite number`);
}

/** Canonical validation for the exact status variants emitted by markShadowPosition. */
export function validateShadowMark(value: unknown, path = 'shadow mark'): ShadowMark {
  const mark = markRecord(value, path);
  markString(mark.sourceBaseId, `${path}.sourceBaseId`);
  markString(mark.coin, `${path}.coin`);
  if (mark.side !== 'long' && mark.side !== 'short') throw new Error(`${path}.side is invalid`);
  markNumber(mark.size, `${path}.size`, true);
  markNumber(mark.markedAtMs, `${path}.markedAtMs`);
  if ((mark.markedAtMs as number) <= 0) throw new Error(`${path}.markedAtMs must be positive`);
  const status = mark.status;
  const statuses = ['MARKED', 'PARTIAL_DEPTH', 'INCOMPLETE_LEGACY_ENTRY', 'BOOK_REJECTED', 'FUNDING_UNAVAILABLE'];
  if (typeof status !== 'string' || !statuses.includes(status)) throw new Error(`${path}.status is invalid`);
  for (const key of ['reason', 'executionEvidenceVersion', 'costModelVersion']) {
    if (mark[key] !== undefined) markString(mark[key], `${path}.${key}`);
  }
  if (status !== 'INCOMPLETE_LEGACY_ENTRY') {
    if (typeof mark.size !== 'number' || !Number.isFinite(mark.size) || mark.size <= 0) throw new Error(`${path}.size is invalid`);
    markString(mark.executionEvidenceVersion, `${path}.executionEvidenceVersion`);
    markString(mark.costModelVersion, `${path}.costModelVersion`);
  }
  if (status === 'INCOMPLETE_LEGACY_ENTRY' || status === 'BOOK_REJECTED' || status === 'FUNDING_UNAVAILABLE') {
    markString(mark.reason, `${path}.reason`);
  }
  if (status === 'FUNDING_UNAVAILABLE') {
    for (const key of ['entryPrice', 'markExitPrice', 'markedSize', 'unfilledSize', 'spreadBps', 'slippageBps',
      'bookAgeMs', 'bookTimeMs']) markNumber(mark[key], `${path}.${key}`);
  }
  if (status === 'MARKED' || status === 'PARTIAL_DEPTH') {
    for (const key of ['entryPrice', 'markExitPrice', 'markedSize', 'unfilledSize', 'grossPnlUsd', 'netPnlUsd',
      'grossReturnBps', 'netReturnBps', 'fundingUsd', 'entryFeeUsd', 'exitFeeUsd', 'spreadBps', 'slippageBps',
      'bookAgeMs', 'bookTimeMs']) markNumber(mark[key], `${path}.${key}`);
  }
  return mark as unknown as ShadowMark;
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
