export type ShadowSide = 'long' | 'short';
export type BookAction = 'buy' | 'sell';

export interface RawBookLevel {
  px?: string | number;
  sz?: string | number;
  n?: number;
}

export interface RawL2Book {
  coin?: string;
  time?: number;
  levels?: RawBookLevel[][];
}

export interface BookLevel {
  px: number;
  sz: number;
}

export interface BookSnapshot {
  coin: string;
  bookTimeMs: number;
  requestedAtMs: number;
  receivedAtMs: number;
  bids: BookLevel[];
  asks: BookLevel[];
}

export interface ShadowExecutionPolicy {
  maxBookAgeMs: number;
  maxSpreadBps: number;
  minNotionalUsd: number;
  takerFeeBps: number;
  fundingOracleMaxDelayMs?: number;
}

export interface ShadowFill {
  requestedRawSize: number;
  requestedRoundedSize: number;
  filledSize: number;
  unfilledSize: number;
  partial: boolean;
  avgPx: number;
  notionalUsd: number;
  midPx: number;
  spreadBps: number;
  slippageBps: number;
  slippageUsd: number;
  feeUsd: number;
  levelsConsumed: number;
  bookAgeMs: number;
  bookTimeMs: number;
  requestedAtMs: number;
  receivedAtMs: number;
}

export type ShadowFillResult =
  | { ok: true; fill: ShadowFill }
  | {
      ok: false;
      reason:
        | 'missing_book'
        | 'stale_book'
        | 'spread_too_wide'
        | 'zero_depth'
        | 'lot_rounded_to_zero'
        | 'below_min_notional';
      detail?: Record<string, number | string | null>;
    };

export interface FundingPoint {
  timeMs: number;
  rate: number;
}

export interface ExposureCheckpoint {
  atMs: number;
  size: number;
}

export interface FundingOracleCheckpoint {
  fundingTimeMs: number;
  observedAtMs: number;
  oraclePx: number;
}

export interface FundingCostResult {
  fundingUsd: number;
  fundingPoints: number;
  oraclePointsMatched: number;
  maxOracleDelayMs: number;
}

export interface PositionEconomicsInput {
  side: ShadowSide;
  entryAvgPx: number;
  size: number;
  entryFeeUsd: number;
  entryNotionalUsd: number;
  exitFill: ShadowFill;
  fundingUsd: number;
}

export interface PositionEconomics {
  grossPnlUsd: number;
  entryFeeUsd: number;
  exitFeeUsd: number;
  fundingUsd: number;
  totalExplicitCostUsd: number;
  netPnlUsd: number;
  grossReturnBps: number;
  netReturnBps: number;
}

function finitePositive(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : null;
}

export function normalizeL2Book(
  raw: RawL2Book | null | undefined,
  requestedAtMs: number,
  receivedAtMs: number,
  fallbackCoin = '',
): BookSnapshot | null {
  if (!raw || !Array.isArray(raw.levels) || raw.levels.length < 2) return null;
  const bookTimeMs = Number(raw.time);
  if (!Number.isFinite(bookTimeMs) || bookTimeMs <= 0) return null;

  const parse = (levels: RawBookLevel[] | undefined) => (levels ?? [])
    .map(level => ({ px: finitePositive(level?.px), sz: finitePositive(level?.sz) }))
    .filter((level): level is { px: number; sz: number } => level.px != null && level.sz != null)
    .map(level => ({ px: level.px, sz: level.sz }));

  const bids = parse(raw.levels[0]).sort((a, b) => b.px - a.px);
  const asks = parse(raw.levels[1]).sort((a, b) => a.px - b.px);
  return {
    coin: String(raw.coin ?? fallbackCoin),
    bookTimeMs,
    requestedAtMs,
    receivedAtMs,
    bids,
    asks,
  };
}

export function roundSizeDown(rawSize: number, szDecimals: number): number {
  if (!Number.isFinite(rawSize) || rawSize <= 0) return 0;
  if (!Number.isInteger(szDecimals) || szDecimals < 0 || szDecimals > 12) {
    throw new Error(`Invalid szDecimals: ${szDecimals}`);
  }
  const factor = 10 ** szDecimals;
  const scaled = rawSize * factor;
  // Correct only floating-point representation error around an exact lot boundary.
  // Number.EPSILON is relative to values near 1, so scale it to the magnitude being floored.
  const ulpAllowance = Math.max(1, Math.abs(scaled)) * Number.EPSILON * 8;
  return Math.floor(scaled + ulpAllowance) / factor;
}

export function isNonExecutableDust(
  rawSize: number,
  szDecimals: number,
  referencePrice: number,
  minNotionalUsd: number,
): boolean {
  if (!(rawSize > 0) || !(referencePrice > 0) || !(minNotionalUsd > 0)) return false;
  const rounded = roundSizeDown(rawSize, szDecimals);
  return !(rounded > 0) || rounded * referencePrice < minNotionalUsd;
}

export function simulateL2Fill(
  book: BookSnapshot | null,
  action: BookAction,
  rawSize: number,
  szDecimals: number,
  policy: ShadowExecutionPolicy,
): ShadowFillResult {
  if (!book) return { ok: false, reason: 'missing_book' };
  const bookAgeMs = book.receivedAtMs - book.bookTimeMs;
  if (bookAgeMs < 0 || bookAgeMs > policy.maxBookAgeMs) {
    return {
      ok: false,
      reason: 'stale_book',
      detail: { bookAgeMs, maxBookAgeMs: policy.maxBookAgeMs },
    };
  }
  if (!book.bids.length || !book.asks.length) return { ok: false, reason: 'zero_depth' };

  const bestBid = book.bids[0].px;
  const bestAsk = book.asks[0].px;
  const midPx = (bestBid + bestAsk) / 2;
  const spreadBps = ((bestAsk - bestBid) / midPx) * 10_000;
  if (!Number.isFinite(spreadBps) || spreadBps > policy.maxSpreadBps) {
    return {
      ok: false,
      reason: 'spread_too_wide',
      detail: { spreadBps, maxSpreadBps: policy.maxSpreadBps },
    };
  }

  const requestedRoundedSize = roundSizeDown(rawSize, szDecimals);
  if (!(requestedRoundedSize > 0)) {
    return {
      ok: false,
      reason: 'lot_rounded_to_zero',
      detail: { rawSize, szDecimals },
    };
  }
  if (requestedRoundedSize * midPx < policy.minNotionalUsd) {
    return {
      ok: false,
      reason: 'below_min_notional',
      detail: {
        requestedNotionalUsd: requestedRoundedSize * midPx,
        minNotionalUsd: policy.minNotionalUsd,
      },
    };
  }

  const levels = action === 'buy' ? book.asks : book.bids;
  let remaining = requestedRoundedSize;
  let filledSize = 0;
  let quoteUsd = 0;
  let levelsConsumed = 0;
  for (const level of levels) {
    if (!(remaining > 0)) break;
    const take = Math.min(remaining, level.sz);
    if (!(take > 0)) continue;
    filledSize += take;
    quoteUsd += take * level.px;
    remaining -= take;
    levelsConsumed += 1;
  }
  if (!(filledSize > 0) || !(quoteUsd > 0)) return { ok: false, reason: 'zero_depth' };

  const avgPx = quoteUsd / filledSize;
  const notionalUsd = quoteUsd;
  if (notionalUsd < policy.minNotionalUsd) {
    return {
      ok: false,
      reason: 'below_min_notional',
      detail: { filledNotionalUsd: notionalUsd, minNotionalUsd: policy.minNotionalUsd },
    };
  }
  const adversePx = action === 'buy' ? avgPx - midPx : midPx - avgPx;
  const slippageBps = Math.max(0, (adversePx / midPx) * 10_000);
  const slippageUsd = Math.max(0, adversePx * filledSize);
  // Compare coverage to the caller's true requested exposure, not only the lot-rounded amount.
  // This makes any sub-lot residual visible instead of incorrectly labelling the mark complete.
  const unfilledSize = Math.max(0, rawSize - filledSize);

  return {
    ok: true,
    fill: {
      requestedRawSize: rawSize,
      requestedRoundedSize,
      filledSize,
      unfilledSize,
      partial: unfilledSize > Math.max(1e-12, rawSize * 1e-12),
      avgPx,
      notionalUsd,
      midPx,
      spreadBps,
      slippageBps,
      slippageUsd,
      feeUsd: notionalUsd * (policy.takerFeeBps / 10_000),
      levelsConsumed,
      bookAgeMs,
      bookTimeMs: book.bookTimeMs,
      requestedAtMs: book.requestedAtMs,
      receivedAtMs: book.receivedAtMs,
    },
  };
}

export function closeAction(side: ShadowSide): BookAction {
  return side === 'long' ? 'sell' : 'buy';
}

export function openAction(side: ShadowSide): BookAction {
  return side === 'long' ? 'buy' : 'sell';
}

export function fundingCostUsd(
  side: ShadowSide,
  checkpoints: ExposureCheckpoint[],
  history: FundingPoint[],
  oracleCheckpoints: FundingOracleCheckpoint[],
  maxOracleDelayMs: number,
): FundingCostResult {
  if (!Number.isFinite(maxOracleDelayMs) || maxOracleDelayMs < 0) {
    throw new Error(`Invalid maxOracleDelayMs: ${maxOracleDelayMs}`);
  }
  const orderedExposure = [...checkpoints]
    .filter(p => Number.isFinite(p.atMs) && Number.isFinite(p.size) && p.size >= 0)
    .sort((a, b) => a.atMs - b.atMs);
  if (!orderedExposure.length) throw new Error('No valid size checkpoints for funding accounting');

  const oracleByFundingTime = new Map<number, FundingOracleCheckpoint>();
  for (const checkpoint of oracleCheckpoints) {
    if (
      !Number.isFinite(checkpoint.fundingTimeMs)
      || !Number.isFinite(checkpoint.observedAtMs)
      || !(checkpoint.oraclePx > 0)
    ) continue;
    const delayMs = checkpoint.observedAtMs - checkpoint.fundingTimeMs;
    if (delayMs < 0 || delayMs > maxOracleDelayMs) continue;
    const prior = oracleByFundingTime.get(checkpoint.fundingTimeMs);
    if (!prior || checkpoint.observedAtMs < prior.observedAtMs) {
      oracleByFundingTime.set(checkpoint.fundingTimeMs, checkpoint);
    }
  }

  const validHistory = history
    .filter(point => Number.isFinite(point.timeMs) && Number.isFinite(point.rate))
    .sort((a, b) => a.timeMs - b.timeMs);
  const historyTimes = new Set(validHistory.map(point => point.timeMs));

  // Funding evidence must be complete in both directions. A prospective oracle checkpoint
  // proves that an active position crossed a funding boundary; silently accepting a missing
  // funding-history row would turn an unknown cost/credit into optimistic zero.
  for (const fundingTimeMs of oracleByFundingTime.keys()) {
    let active: ExposureCheckpoint | null = null;
    for (const checkpoint of orderedExposure) {
      if (checkpoint.atMs <= fundingTimeMs) active = checkpoint;
      else break;
    }
    if (active && active.size > 0 && !historyTimes.has(fundingTimeMs)) {
      throw new Error(`Missing funding-history row for captured oracle interval ${fundingTimeMs}`);
    }
  }

  if (!validHistory.length) {
    return { fundingUsd: 0, fundingPoints: 0, oraclePointsMatched: 0, maxOracleDelayMs };
  }

  const sign = side === 'long' ? 1 : -1;
  let total = 0;
  let fundingPoints = 0;
  let oraclePointsMatched = 0;
  for (const point of validHistory) {
    let active: ExposureCheckpoint | null = null;
    for (const checkpoint of orderedExposure) {
      if (checkpoint.atMs <= point.timeMs) active = checkpoint;
      else break;
    }
    if (!active || !(active.size > 0)) continue;
    fundingPoints += 1;
    const oracle = oracleByFundingTime.get(point.timeMs);
    if (!oracle) {
      throw new Error(`Missing fresh oracle checkpoint for funding interval ${point.timeMs}`);
    }
    oraclePointsMatched += 1;
    total += active.size * oracle.oraclePx * point.rate * sign;
  }
  return { fundingUsd: total, fundingPoints, oraclePointsMatched, maxOracleDelayMs };
}

export function computePositionEconomics(input: PositionEconomicsInput): PositionEconomics {
  const direction = input.side === 'long' ? 1 : -1;
  const grossPnlUsd = (input.exitFill.avgPx - input.entryAvgPx) * input.size * direction;
  const grossReturnBps = input.entryAvgPx > 0
    ? ((input.exitFill.avgPx - input.entryAvgPx) / input.entryAvgPx) * direction * 10_000
    : 0;
  const totalExplicitCostUsd = input.entryFeeUsd + input.exitFill.feeUsd + input.fundingUsd;
  const netPnlUsd = grossPnlUsd - totalExplicitCostUsd;
  const netReturnBps = input.entryNotionalUsd > 0
    ? (netPnlUsd / input.entryNotionalUsd) * 10_000
    : 0;
  return {
    grossPnlUsd,
    entryFeeUsd: input.entryFeeUsd,
    exitFeeUsd: input.exitFill.feeUsd,
    fundingUsd: input.fundingUsd,
    totalExplicitCostUsd,
    netPnlUsd,
    grossReturnBps,
    netReturnBps,
  };
}
