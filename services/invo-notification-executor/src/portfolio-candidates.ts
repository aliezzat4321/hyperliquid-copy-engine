import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from 'fs';
import { dirname } from 'path';
import { FEED_EVIDENCE_EPOCH, FEED_EVIDENCE_SELECTOR_TTL_MS, type FeedPortfolioRecord } from './feed-portfolio-evidence.js';

export const ELITE_SELECTOR_VERSION = 'invo-portfolio-hybrid-v4-20260921';

export type PortfolioBucket =
  | 'ELITE_CANDIDATE'
  | 'SPARSE_HIGH_RETURN'
  | 'RESEARCH_WIDE'
  | 'REJECTED_DEMOTED';

export interface PortfolioSelectorPolicy {
  minClosedPositions: number;
  minDaysActive: number;
  minWinRatePct: number;
  minPercentChange: number;
  minQualityScore: number;
  sparseMinClosedPositions: number;
  sparseMinWinRatePct: number;
  sparseMinPercentChange: number;
}

export const DEFAULT_PORTFOLIO_SELECTOR: PortfolioSelectorPolicy = Object.freeze({
  // Broad discovery stays permissive, but shadow admission uses a hybrid
  // win-rate/return curve plus sample confidence. 60% is the absolute quality floor.
  minClosedPositions: 20,
  minDaysActive: 7,
  minWinRatePct: 60,
  minPercentChange: 0.01,
  minQualityScore: 60,
  sparseMinClosedPositions: 8,
  sparseMinWinRatePct: 60,
  sparseMinPercentChange: 100,
});

// Hybrid admission anchors. Return requirements decay exponentially as win rate rises,
// while sample requirements rise linearly as win rate falls below 80%.
const HYBRID_REFERENCE_WIN_RATE_PCT = 80;
const HYBRID_REFERENCE_RETURN_PCT = 100;
const HYBRID_RETURN_AT_WIN_RATE_FLOOR_PCT = 1000;
const HYBRID_CLOSED_AT_WIN_RATE_FLOOR = 50;

export interface PortfolioScoreBreakdown {
  winRate: number;
  historicalReturn: number;
  sampleSize: number;
  activeDays: number | null;
  dailyFrequency: number | null;
  recentActivity: number | null;
  availableWeight: number;
}

export interface PortfolioSnapshot {
  observedAtMs: number;
  sourceFilter: string;
  portfolioId: string;
  portfolioName: string | null;
  ownerId: string | null;
  username: string | null;
  verified: boolean | null;
  createdAtMs: number | null;
  daysActive: number | null;
  lastActivityAtMs: number | null;
  recentActivityDaysAgo: number | null;
  openPositions: number | null;
  closedPositions: number;
  closedPositionsPerDay: number | null;
  wonPositions: number;
  lostPositions: number;
  winRatePct: number | null;
  percentChange: number | null;
  hybridRequiredReturnPct: number | null;
  hybridRequiredClosedPositions: number | null;
  winLossRatio: number | null;
  currentWinStreak: number | null;
  followerCount: number | null;
  liquidated: boolean | null;
  bucket: PortfolioBucket;
  score: number;
  scoreBreakdown: PortfolioScoreBreakdown;
  selectorVersion: string;
  reasons: string[];
  rawShapeKeys: string[];
}

export interface LedgerDiskState {
  version: 1;
  selectorVersion: string;
  policy: PortfolioSelectorPolicy;
  portfolios: Record<string, PortfolioSnapshot>;
  firstEliteAtMs: Record<string, number>;
  lastObservedAtMs: number;
  feedEvidence?: {
    version: 4;
    epoch: typeof FEED_EVIDENCE_EPOCH;
    eligibilityNotBeforeMs: number;
    records: Record<string, { firstProcessedAtMs: number; lastProcessedAtMs: number; sourceFirstSeenAtMs: number; sourceLastSeenAtMs: number; surfaces: string[]; processedEvidenceIds: string[]; feedOnlyAtFirstIngestion: boolean; newlySelectorQualified: boolean }>;
    lifetime: { discovered: number; feedOnly: number; newlyQualified: number; rejectedUnverified: number; rejectedMalformed: number; rejectedIdentityConflicts: number; dedupedReplayCount: number };
  };
}

export const FEED_SELECTOR_TRACKING_MAX_PORTFOLIOS = 5_000;
export const CANDIDATE_STATE_MAX_PORTFOLIOS = 5_000;
export const CANDIDATE_STATE_MAX_BYTES = 8 * 1024 * 1024;
export const CANDIDATE_SNAPSHOT_SEGMENT_MAX_BYTES = 4 * 1024 * 1024;
export const CANDIDATE_SNAPSHOT_TOTAL_MAX_BYTES = 8 * 1024 * 1024;
const emptyFeedEvidence = (eligibilityNotBeforeMs = 0) => ({
  version: 4 as const, epoch: FEED_EVIDENCE_EPOCH as typeof FEED_EVIDENCE_EPOCH,
  eligibilityNotBeforeMs,
  records: {} as Record<string, { firstProcessedAtMs: number; lastProcessedAtMs: number; sourceFirstSeenAtMs: number; sourceLastSeenAtMs: number; surfaces: string[]; processedEvidenceIds: string[]; feedOnlyAtFirstIngestion: boolean; newlySelectorQualified: boolean }>,
  lifetime: { discovered: 0, feedOnly: 0, newlyQualified: 0, rejectedUnverified: 0, rejectedMalformed: 0, rejectedIdentityConflicts: 0, dedupedReplayCount: 0 },
});

const PORTFOLIO_BUCKETS = new Set<PortfolioBucket>([
  'ELITE_CANDIDATE', 'SPARSE_HIGH_RETURN', 'RESEARCH_WIDE', 'REJECTED_DEMOTED',
]);

export const ALLOWED_PORTFOLIO_BUCKETS: ReadonlySet<PortfolioBucket> = PORTFOLIO_BUCKETS;

function plainRecord(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === 'object' && !Array.isArray(value)
    && [Object.prototype, null].includes(Object.getPrototypeOf(value));
}
function finiteNumber(value: unknown, positive = false): value is number {
  return typeof value === 'number' && Number.isFinite(value) && (!positive || value > 0);
}
const POLICY_KEYS = Object.keys(DEFAULT_PORTFOLIO_SELECTOR).sort();

/** Exact persisted candidate-state envelope used at admission boundaries. */
export function isCanonicalLedgerDiskState(value: unknown): value is LedgerDiskState {
  if (!plainRecord(value) || value.version !== 1 || value.selectorVersion !== ELITE_SELECTOR_VERSION
    || !plainRecord(value.policy) || !plainRecord(value.portfolios) || !plainRecord(value.firstEliteAtMs)
    || !finiteNumber(value.lastObservedAtMs, true) || !plainRecord(value.feedEvidence)) return false;
  if (Object.keys(value).some(key => !['version', 'selectorVersion', 'policy', 'portfolios', 'firstEliteAtMs',
    'lastObservedAtMs', 'feedEvidence'].includes(key))) return false;
  const policy = value.policy;
  if (Object.keys(policy).sort().join('\0') !== POLICY_KEYS.join('\0')
    || POLICY_KEYS.some(key => !finiteNumber(policy[key]))) return false;
  if (!Object.entries(value.portfolios).every(([key, row]) => isCanonicalPortfolioSnapshot(row)
    && row.portfolioId === key)) return false;
  if (!Object.entries(value.firstEliteAtMs).every(([key, at]) => key.trim().length > 0 && finiteNumber(at, true))) return false;
  const feed = value.feedEvidence;
  if (feed.version !== 4 || feed.epoch !== FEED_EVIDENCE_EPOCH || !finiteNumber(feed.eligibilityNotBeforeMs)
    || !plainRecord(feed.records) || !plainRecord(feed.lifetime)) return false;
  const lifetime = feed.lifetime;
  const lifetimeKeys = ['discovered', 'feedOnly', 'newlyQualified', 'rejectedUnverified', 'rejectedMalformed',
    'rejectedIdentityConflicts', 'dedupedReplayCount'];
  if (Object.keys(lifetime).sort().join('\0') !== [...lifetimeKeys].sort().join('\0')
    || lifetimeKeys.some(key => !finiteNumber(lifetime[key]) || !Number.isInteger(lifetime[key])
      || (lifetime[key] as number) < 0)) return false;
  return Object.entries(feed.records).every(([key, raw]) => {
    if (!key.trim() || !plainRecord(raw)) return false;
    const keys = ['firstProcessedAtMs', 'lastProcessedAtMs', 'sourceFirstSeenAtMs', 'sourceLastSeenAtMs',
      'surfaces', 'processedEvidenceIds', 'feedOnlyAtFirstIngestion', 'newlySelectorQualified'];
    return !Object.keys(raw).some(field => !keys.includes(field))
      && ['firstProcessedAtMs', 'lastProcessedAtMs', 'sourceFirstSeenAtMs', 'sourceLastSeenAtMs']
        .every(field => finiteNumber(raw[field], true))
      && Array.isArray(raw.surfaces) && raw.surfaces.every(item => typeof item === 'string' && item.length > 0)
      && Array.isArray(raw.processedEvidenceIds) && raw.processedEvidenceIds.every(item => typeof item === 'string' && item.length > 0)
      && typeof raw.feedOnlyAtFirstIngestion === 'boolean' && typeof raw.newlySelectorQualified === 'boolean';
  });
}

function nullableFinite(value: unknown): boolean {
  return value === null || (typeof value === 'number' && Number.isFinite(value));
}

/** Exact structural contract emitted by classifyPortfolio and consumed by any
 * authorization/terminal-denial boundary. Keep this stricter than the compact
 * projector snapshot contract: a partial imitation must never authorize or consume. */
export function isCanonicalPortfolioSnapshot(row: unknown): row is PortfolioSnapshot {
  if (row == null || typeof row !== 'object' || Array.isArray(row)) return false;
  const candidate = row as Record<string, unknown>;
  const breakdown = candidate.scoreBreakdown;
  if (breakdown == null || typeof breakdown !== 'object' || Array.isArray(breakdown)) return false;
  const scoreBreakdown = breakdown as Record<string, unknown>;
  return typeof candidate.portfolioId === 'string' && candidate.portfolioId.trim().length > 0
    && typeof candidate.observedAtMs === 'number' && Number.isFinite(candidate.observedAtMs) && candidate.observedAtMs > 0
    && candidate.selectorVersion === ELITE_SELECTOR_VERSION
    && PORTFOLIO_BUCKETS.has(candidate.bucket as PortfolioBucket)
    && typeof candidate.sourceFilter === 'string' && candidate.sourceFilter.trim().length > 0
    && (candidate.portfolioName === null || typeof candidate.portfolioName === 'string')
    && (candidate.ownerId === null || typeof candidate.ownerId === 'string')
    && (candidate.username === null || typeof candidate.username === 'string')
    && (candidate.verified === null || typeof candidate.verified === 'boolean')
    && nullableFinite(candidate.createdAtMs)
    && nullableFinite(candidate.daysActive)
    && nullableFinite(candidate.lastActivityAtMs)
    && nullableFinite(candidate.recentActivityDaysAgo)
    && (candidate.openPositions === null || (typeof candidate.openPositions === 'number'
      && Number.isInteger(candidate.openPositions) && candidate.openPositions >= 0))
    && typeof candidate.closedPositions === 'number' && Number.isInteger(candidate.closedPositions)
      && candidate.closedPositions >= 0
    && nullableFinite(candidate.closedPositionsPerDay)
    && typeof candidate.wonPositions === 'number' && Number.isInteger(candidate.wonPositions) && candidate.wonPositions >= 0
    && typeof candidate.lostPositions === 'number' && Number.isInteger(candidate.lostPositions) && candidate.lostPositions >= 0
    && (candidate.winRatePct === null || (typeof candidate.winRatePct === 'number'
      && Number.isFinite(candidate.winRatePct) && candidate.winRatePct >= 0 && candidate.winRatePct <= 100))
    && nullableFinite(candidate.percentChange)
    && nullableFinite(candidate.hybridRequiredReturnPct)
    && (candidate.hybridRequiredClosedPositions === null
      || (typeof candidate.hybridRequiredClosedPositions === 'number'
        && Number.isInteger(candidate.hybridRequiredClosedPositions) && candidate.hybridRequiredClosedPositions >= 0))
    && nullableFinite(candidate.winLossRatio)
    && nullableFinite(candidate.currentWinStreak)
    && nullableFinite(candidate.followerCount)
    && (candidate.liquidated === null || typeof candidate.liquidated === 'boolean')
    && typeof candidate.score === 'number' && Number.isFinite(candidate.score)
    && typeof scoreBreakdown.winRate === 'number' && Number.isFinite(scoreBreakdown.winRate)
    && typeof scoreBreakdown.historicalReturn === 'number' && Number.isFinite(scoreBreakdown.historicalReturn)
    && typeof scoreBreakdown.sampleSize === 'number' && Number.isFinite(scoreBreakdown.sampleSize)
    && nullableFinite(scoreBreakdown.activeDays)
    && nullableFinite(scoreBreakdown.dailyFrequency)
    && nullableFinite(scoreBreakdown.recentActivity)
    && typeof scoreBreakdown.availableWeight === 'number' && Number.isFinite(scoreBreakdown.availableWeight)
    && Array.isArray(candidate.reasons) && candidate.reasons.every(value => typeof value === 'string')
    && Array.isArray(candidate.rawShapeKeys) && candidate.rawShapeKeys.every(value => typeof value === 'string');
}

function validRecentSnapshot(row: unknown): row is PortfolioSnapshot {
  if (row == null || typeof row !== 'object' || Array.isArray(row)) return false;
  const candidate = row as Partial<PortfolioSnapshot>;
  return typeof candidate.portfolioId === 'string' && candidate.portfolioId.trim().length > 0
    && candidate.selectorVersion === ELITE_SELECTOR_VERSION
    && typeof candidate.observedAtMs === 'number'
    && Number.isFinite(candidate.observedAtMs) && candidate.observedAtMs > 0
    && PORTFOLIO_BUCKETS.has(candidate.bucket as PortfolioBucket);
}

function finite(v: unknown): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function integer(v: unknown, fallback = 0): number {
  const n = finite(v);
  return n == null ? fallback : Math.max(0, Math.trunc(n));
}

function consistentAlias(raw: any, keys: string[]): number | null | undefined {
  const present = keys.map(key => raw?.[key]).filter(value => value !== undefined && value !== null);
  if (!present.length) return null;
  const values = present.map(finite);
  if (values.some(value => value == null)) return undefined;
  const first = values[0]!;
  return values.every(value => Math.abs(value! - first) <= 1e-9) ? first : undefined;
}

function text(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v.trim() : null;
}

function bool(v: unknown): boolean | null {
  return typeof v === 'boolean' ? v : null;
}

function timestamp(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v < 10_000_000_000 ? v * 1000 : v;
  if (typeof v !== 'string' || !v) return null;
  const parsed = Date.parse(v);
  return Number.isFinite(parsed) ? parsed : null;
}

function clamp(v: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, v));
}

function round2(v: number) {
  return Math.round(v * 100) / 100;
}

function linearPoints(value: number, from: number, to: number, maxPoints: number) {
  if (to <= from) return value >= to ? maxPoints : 0;
  return clamp((value - from) / (to - from), 0, 1) * maxPoints;
}

/**
 * Continuous trade-off between win rate and historical return.
 * Default anchors: 60% WR -> 1000% return, 80% WR -> 100% return.
 * Above 80%, required return keeps decaying smoothly; below 60%, admission is impossible.
 */
export function requiredReturnPctForWinRate(
  winRatePct: number,
  policy: PortfolioSelectorPolicy = DEFAULT_PORTFOLIO_SELECTOR,
): number | null {
  if (!Number.isFinite(winRatePct) || winRatePct < policy.minWinRatePct) return null;
  const floorWin = policy.minWinRatePct;
  const referenceWin = Math.max(HYBRID_REFERENCE_WIN_RATE_PCT, floorWin + 1);
  const floorReturn = Math.max(HYBRID_RETURN_AT_WIN_RATE_FLOOR_PCT, policy.minPercentChange);
  const referenceReturn = Math.max(HYBRID_REFERENCE_RETURN_PCT, policy.minPercentChange);
  const t = (winRatePct - floorWin) / (referenceWin - floorWin);
  const logRequired = Math.log1p(floorReturn)
    + (Math.log1p(referenceReturn) - Math.log1p(floorReturn)) * t;
  return round2(Math.max(policy.minPercentChange, Math.expm1(logRequired)));
}

/**
 * Lower-win-rate strategies need more closed-trade evidence before consuming shadow capacity.
 * Default: 60% WR needs 50 closes, declining linearly to the normal 20-close floor at 80% WR.
 */
export function requiredClosedPositionsForWinRate(
  winRatePct: number,
  policy: PortfolioSelectorPolicy = DEFAULT_PORTFOLIO_SELECTOR,
): number | null {
  if (!Number.isFinite(winRatePct) || winRatePct < policy.minWinRatePct) return null;
  if (winRatePct >= HYBRID_REFERENCE_WIN_RATE_PCT) return policy.minClosedPositions;
  const span = HYBRID_REFERENCE_WIN_RATE_PCT - policy.minWinRatePct;
  if (span <= 0) return policy.minClosedPositions;
  const t = clamp((winRatePct - policy.minWinRatePct) / span, 0, 1);
  return Math.ceil(HYBRID_CLOSED_AT_WIN_RATE_FLOOR
    + (policy.minClosedPositions - HYBRID_CLOSED_AT_WIN_RATE_FLOOR) * t);
}

function scorePortfolio(input: {
  closedPositions: number;
  daysActive: number | null;
  winRatePct: number | null;
  percentChange: number | null;
  recentActivityDaysAgo: number | null;
}, policy: PortfolioSelectorPolicy): { score: number; breakdown: PortfolioScoreBreakdown; closedPositionsPerDay: number | null } {
  const closedPositionsPerDay = input.daysActive != null && input.daysActive > 0
    ? input.closedPositions / Math.max(input.daysActive, 1)
    : null;

  // 30 points: win rate is one half of the core quality pair. 60% is only the absolute
  // floor; 80/90/95% progressively earn stronger credit. Return can compensate for a
  // lower (but still >=60%) win rate through the hybrid admission curve below.
  const winRate = input.winRatePct == null
    ? 0
    : input.winRatePct < policy.minWinRatePct
      ? linearPoints(input.winRatePct, 40, policy.minWinRatePct, 10)
      : 10 + linearPoints(input.winRatePct, policy.minWinRatePct, 95, 20);

  // 30 points: historical return is equally important and deliberately saturating.
  // 500% is excellent; 1000% reaches maximum return credit, while the hybrid curve
  // separately determines how much return is required for a given win rate.
  const historicalReturn = input.percentChange == null || input.percentChange <= 0
    ? 0
    : clamp(Math.log1p(input.percentChange) / Math.log1p(1000), 0, 1) * 30;

  // 15 points: 20 trades can qualify at high win rates; lower win rates are separately
  // required to bring a deeper sample before admission.
  const sampleSize = input.closedPositions < 20
    ? linearPoints(input.closedPositions, 0, 20, 5)
    : 5 + linearPoints(input.closedPositions, 20, 100, 10);

  // 10 points: one week is enough to enter research; two weeks and one month add confidence.
  let activeDays: number | null = null;
  if (input.daysActive != null) {
    if (input.daysActive < 7) activeDays = linearPoints(input.daysActive, 0, 7, 4);
    else if (input.daysActive < 14) activeDays = 4 + linearPoints(input.daysActive, 7, 14, 3);
    else activeDays = 7 + linearPoints(input.daysActive, 14, 30, 3);
  }

  // 10 points: reward portfolios that actually trade. About 3 closed trades/day saturates.
  const dailyFrequency = closedPositionsPerDay == null
    ? null
    : linearPoints(closedPositionsPerDay, 0, 3, 10);

  // 5 points: use only an explicit Invo trade/activity timestamp when available.
  // Missing recency data is neutral: it is removed from available weight rather than guessed.
  let recentActivity: number | null = null;
  if (input.recentActivityDaysAgo != null) {
    if (input.recentActivityDaysAgo <= 1) recentActivity = 5;
    else if (input.recentActivityDaysAgo <= 3) recentActivity = 4;
    else if (input.recentActivityDaysAgo <= 7) recentActivity = 3;
    else if (input.recentActivityDaysAgo <= 14) recentActivity = 1.5;
    else recentActivity = 0;
  }

  let earned = winRate + historicalReturn + sampleSize;
  let availableWeight = 75;
  if (activeDays != null) {
    earned += activeDays;
    availableWeight += 10;
  }
  if (dailyFrequency != null) {
    earned += dailyFrequency;
    availableWeight += 10;
  }
  if (recentActivity != null) {
    earned += recentActivity;
    availableWeight += 5;
  }

  const score = availableWeight > 0 ? round2((earned / availableWeight) * 100) : 0;
  return {
    score,
    closedPositionsPerDay: closedPositionsPerDay == null ? null : round2(closedPositionsPerDay),
    breakdown: {
      winRate: round2(winRate),
      historicalReturn: round2(historicalReturn),
      sampleSize: round2(sampleSize),
      activeDays: activeDays == null ? null : round2(activeDays),
      dailyFrequency: dailyFrequency == null ? null : round2(dailyFrequency),
      recentActivity: recentActivity == null ? null : round2(recentActivity),
      availableWeight,
    },
  };
}

export function classifyPortfolio(
  raw: any,
  observedAtMs: number,
  sourceFilter: string,
  policy: PortfolioSelectorPolicy = DEFAULT_PORTFOLIO_SELECTOR,
): PortfolioSnapshot | null {
  const portfolioId = String(raw?.id ?? raw?.portfolioId ?? raw?._id ?? '').trim();
  if (!portfolioId) return null;

  const owner = raw?.owner ?? raw?.user ?? {};
  const ownerId = text(raw?.ownerId ?? owner?.id);
  const username = text(owner?.username ?? raw?.username)?.replace(/^@/, '').toLowerCase() ?? null;
  const portfolioName = text(raw?.name ?? raw?.title ?? raw?.portfolioName);
  const createdAtMs = timestamp(raw?.createdAt ?? raw?.created_at);
  const daysActive = createdAtMs == null ? null : Math.max(0, (observedAtMs - createdAtMs) / 86_400_000);
  const lastActivityAtMs = timestamp(
    raw?.lastTradeAt
      ?? raw?.last_trade_at
      ?? raw?.lastPositionAt
      ?? raw?.last_position_at
      ?? raw?.lastClosedPositionAt
      ?? raw?.last_closed_position_at
      ?? raw?.lastActivityAt
      ?? raw?.last_activity_at,
  );
  const recentActivityDaysAgo = lastActivityAtMs == null
    ? null
    : Math.max(0, (observedAtMs - lastActivityAtMs) / 86_400_000);
  const closedAlias = consistentAlias(raw, ['closedPositions','closedPositionsCount','closedTrades','totalClosedPositions']);
  const wonAlias = consistentAlias(raw, ['wonPositions','wonPositionsCount','winningPositions','wins']);
  const lostAlias = consistentAlias(raw, ['lostPositions','lostPositionsCount','losingPositions','losses']);
  const winRateAlias = consistentAlias(raw, ['winRate','win_rate','winRatePct']);
  const returnAlias = consistentAlias(raw, ['percentChange','pnlPercent','profitLossPercent','roi']);
  const aliasesConsistent = closedAlias !== undefined && wonAlias !== undefined && lostAlias !== undefined
    && winRateAlias !== undefined && returnAlias !== undefined;
  const closedPositions = integer(closedAlias);
  const openPositions = finite(raw?.openPositions ?? raw?.openPositionsCount ?? raw?.openTrades);
  const wonPositions = integer(wonAlias);
  const lostPositions = integer(lostAlias);
  const winRatePct = winRateAlias ?? null;
  const percentChange = returnAlias ?? null;
  const liquidated = bool(raw?.liquidated ?? raw?.isLiquidated);
  const verified = bool(owner?.verified ?? raw?.verified ?? raw?.isVerified);
  const currentWinStreak = finite(raw?.currentWinStreak ?? raw?.winStreak);
  const followerCount = finite(raw?.followerCount ?? raw?.followers);
  // This is a count ratio, not an average-win / average-loss payout ratio. Keep it
  // for observability, but do not double-count it as independent profitability evidence.
  const winLossRatio = lostPositions > 0 ? wonPositions / lostPositions : wonPositions > 0 ? wonPositions : null;
  const metricsComplete = createdAtMs != null && winRatePct != null && percentChange != null
    && Number.isFinite(Number(raw?.closedPositions ?? raw?.closedPositionsCount ?? raw?.closedTrades ?? raw?.totalClosedPositions))
    && Number.isFinite(Number(raw?.wonPositions ?? raw?.wonPositionsCount ?? raw?.winningPositions ?? raw?.wins))
    && Number.isFinite(Number(raw?.lostPositions ?? raw?.lostPositionsCount ?? raw?.losingPositions ?? raw?.losses));
  const metricsConsistent = metricsComplete && aliasesConsistent && wonPositions + lostPositions <= closedPositions
    && winRatePct! >= 0 && winRatePct! <= 100
    && (closedPositions === 0 || Math.abs((wonPositions / closedPositions) * 100 - winRatePct!) <= 1.0);

  const scored = scorePortfolio({
    closedPositions,
    daysActive,
    winRatePct,
    percentChange,
    recentActivityDaysAgo,
  }, policy);

  const reasons: string[] = [];
  let bucket: PortfolioBucket;

  const hardReject = liquidated === true
    || (closedPositions >= policy.minClosedPositions && winRatePct != null && winRatePct < 40)
    || (closedPositions >= policy.minClosedPositions && percentChange != null && percentChange < 0);
  const enoughAge = daysActive == null || daysActive >= policy.minDaysActive;
  const enoughBaseSample = closedPositions >= policy.minClosedPositions;
  const positiveReturn = percentChange != null && percentChange >= policy.minPercentChange;
  const enoughWinRate = winRatePct != null && winRatePct >= policy.minWinRatePct;
  const hybridRequiredReturnPct = winRatePct == null ? null : requiredReturnPctForWinRate(winRatePct, policy);
  const hybridRequiredClosedPositions = winRatePct == null ? null : requiredClosedPositionsForWinRate(winRatePct, policy);
  const enoughHybridReturn = hybridRequiredReturnPct != null
    && percentChange != null
    && percentChange >= hybridRequiredReturnPct;
  const enoughHybridSample = hybridRequiredClosedPositions != null
    && closedPositions >= hybridRequiredClosedPositions;
  const elite = !hardReject
    && (!sourceFilter.startsWith('feed:') || verified === true)
    && metricsComplete
    && metricsConsistent
    && enoughBaseSample
    && enoughAge
    && enoughWinRate
    && positiveReturn
    && enoughHybridReturn
    && enoughHybridSample
    && scored.score >= policy.minQualityScore;

  if (elite) {
    bucket = 'ELITE_CANDIDATE';
    reasons.push('meets_hybrid_win_rate_return_gate_v3');
  } else if (
    !hardReject
    && closedPositions >= policy.sparseMinClosedPositions
    && closedPositions < policy.minClosedPositions
    && winRatePct != null && winRatePct >= policy.sparseMinWinRatePct
    && percentChange != null && percentChange >= policy.sparseMinPercentChange
  ) {
    bucket = 'SPARSE_HIGH_RETURN';
    reasons.push('promising_but_below_20_closed_sample');
  } else if (hardReject) {
    bucket = 'REJECTED_DEMOTED';
    if (liquidated === true) reasons.push('liquidated');
    if (closedPositions >= policy.minClosedPositions && winRatePct != null && winRatePct < 40) reasons.push('very_low_win_rate');
    if (closedPositions >= policy.minClosedPositions && percentChange != null && percentChange < 0) reasons.push('negative_historical_return');
  } else {
    bucket = 'RESEARCH_WIDE';
    if (!enoughBaseSample) reasons.push('below_20_closed_sample');
    if (!enoughAge) reasons.push('below_7_active_days');
    if (winRatePct == null) reasons.push('missing_win_rate');
    else if (!enoughWinRate) reasons.push('win_rate_below_absolute_floor');
    if (percentChange == null) reasons.push('missing_return');
    else if (!positiveReturn) reasons.push('return_not_positive');
    if (enoughWinRate && hybridRequiredReturnPct != null && !enoughHybridReturn) reasons.push('return_below_win_rate_tradeoff');
    if (enoughWinRate && hybridRequiredClosedPositions != null && !enoughHybridSample) reasons.push('sample_below_win_rate_tradeoff');
    if (scored.score < policy.minQualityScore) reasons.push('weighted_quality_score_below_gate');
    if (sourceFilter.startsWith('feed:') && verified !== true) reasons.push('verified_profile_required');
    if (!metricsComplete) reasons.push('incomplete_profile_metrics');
    else if (!metricsConsistent) reasons.push('inconsistent_profile_metrics');
  }

  return {
    observedAtMs,
    sourceFilter,
    portfolioId,
    portfolioName,
    ownerId,
    username,
    verified,
    createdAtMs,
    daysActive: daysActive == null ? null : round2(daysActive),
    lastActivityAtMs,
    recentActivityDaysAgo: recentActivityDaysAgo == null ? null : round2(recentActivityDaysAgo),
    openPositions: openPositions == null ? null : Math.max(0, Math.trunc(openPositions)),
    closedPositions,
    closedPositionsPerDay: scored.closedPositionsPerDay,
    wonPositions,
    lostPositions,
    winRatePct,
    percentChange,
    hybridRequiredReturnPct,
    hybridRequiredClosedPositions,
    winLossRatio: winLossRatio == null ? null : Math.round(winLossRatio * 1000) / 1000,
    currentWinStreak,
    followerCount,
    liquidated,
    bucket,
    score: scored.score,
    scoreBreakdown: scored.breakdown,
    selectorVersion: ELITE_SELECTOR_VERSION,
    reasons,
    rawShapeKeys: Object.keys(raw ?? {}).sort(),
  };
}

export class PortfolioCandidateLedger {
  private state: LedgerDiskState;
  private recentRows: PortfolioSnapshot[] = [];

  constructor(
    private readonly statePath: string,
    private readonly snapshotsPath: string,
    private readonly policy: PortfolioSelectorPolicy = DEFAULT_PORTFOLIO_SELECTOR,
  ) {
    this.state = {
      version: 1,
      selectorVersion: ELITE_SELECTOR_VERSION,
      policy,
      portfolios: {},
      firstEliteAtMs: {},
      lastObservedAtMs: 0,
      // Missing state has no selector eligibility until the first research assimilation,
      // which atomically sets eligibilityNotBeforeMs to that cycle's processedAtMs.
      feedEvidence: emptyFeedEvidence(),
    };
    if (existsSync(statePath)) {
      const raw = readFileSync(statePath, 'utf8');
      if (Buffer.byteLength(raw) > CANDIDATE_STATE_MAX_BYTES) {
        throw new Error('candidate state byte cap exceeded on load');
      }
      try {
        const parsed = JSON.parse(raw) as Partial<LedgerDiskState>;
        const selectorMatches = parsed.selectorVersion === ELITE_SELECTOR_VERSION;
        const parsedPortfolios = selectorMatches ? (parsed.portfolios ?? {}) : {};
        if (Object.keys(parsedPortfolios).length > CANDIDATE_STATE_MAX_PORTFOLIOS) {
          throw new Error('candidate state portfolio cap exceeded on load');
        }
        this.state = {
          version: 1,
          selectorVersion: ELITE_SELECTOR_VERSION,
          policy,
          portfolios: parsedPortfolios,
          firstEliteAtMs: selectorMatches ? (parsed.firstEliteAtMs ?? {}) : {},
          lastObservedAtMs: selectorMatches ? (parsed.lastObservedAtMs ?? 0) : 0,
          feedEvidence: selectorMatches && parsed.feedEvidence?.version === 4 && parsed.feedEvidence.epoch === FEED_EVIDENCE_EPOCH
            ? parsed.feedEvidence : emptyFeedEvidence(Date.now()),
        };
      } catch (error) {
        if (error instanceof Error && error.message.includes('candidate state portfolio cap exceeded')) throw error;
        // Malformed state cannot become selector eligibility evidence.
        this.state = {
          version: 1, selectorVersion: ELITE_SELECTOR_VERSION, policy, portfolios: {},
          firstEliteAtMs: {}, lastObservedAtMs: 0, feedEvidence: emptyFeedEvidence(Date.now()),
        };
      }
    }
    const recentPath = `${snapshotsPath}.recent.json`;
    if (existsSync(recentPath)) {
      try {
        const parsed = JSON.parse(readFileSync(recentPath, 'utf8'));
        if (parsed?.version === 1 && Array.isArray(parsed.rows)
          && (parsed.selectorVersion == null || parsed.selectorVersion === ELITE_SELECTOR_VERSION)) {
          this.recentRows = parsed.rows.filter(validRecentSnapshot);
        }
      } catch {
        // Rebuilt prospectively on the next observation; admission fails closed meanwhile.
      }
    }
  }

  /**
   * Assimilate executor-owned evidence at research processing time. Source timestamps
   * remain provenance only and can never backdate selector visibility.
   */
  assimilateFeedEvidence(records: FeedPortfolioRecord[], processedAtMs = Date.now()) {
    const meta = this.state.feedEvidence ?? emptyFeedEvidence();
    // Missing candidate state starts one durable causal selector boundary. Retained
    // evidence before this processing instant is rejected, but the boundary must not
    // move forward again on the next research cycle or fresh evidence could starve.
    const initializedEligibilityBoundary = !(meta.eligibilityNotBeforeMs > 0);
    if (initializedEligibilityBoundary) meta.eligibilityNotBeforeMs = processedAtMs;
    let observationsProcessed = 0;
    for (const record of records.sort((a, b) => a.firstSeenAtMs - b.firstSeenAtMs || a.portfolioId.localeCompare(b.portfolioId))) {
      const wasKnown = this.state.portfolios[record.portfolioId] != null;
      const wasElite = this.state.portfolios[record.portfolioId]?.bucket === 'ELITE_CANDIDATE';
      const previousMeta = meta.records[record.portfolioId];
      const processedIds = new Set(previousMeta?.processedEvidenceIds ?? []);
      const unseen = record.observations.filter(row => {
        if (row.epoch !== FEED_EVIDENCE_EPOCH || row.processedAtMs < meta.eligibilityNotBeforeMs
          || row.processedAtMs < processedAtMs - FEED_EVIDENCE_SELECTOR_TTL_MS) return false;
        if (!processedIds.has(row.evidenceId)) return true;
        meta.lifetime.dedupedReplayCount += 1;
        return false;
      })
        .sort((a, b) => a.capturedAtMs - b.capturedAtMs || a.evidenceId.localeCompare(b.evidenceId));
      if (!unseen.length) continue;
      if (!previousMeta) { meta.lifetime.discovered += 1; if (!wasKnown) meta.lifetime.feedOnly += 1; }
      meta.records[record.portfolioId] = {
        firstProcessedAtMs: previousMeta?.firstProcessedAtMs ?? processedAtMs, lastProcessedAtMs: processedAtMs,
        sourceFirstSeenAtMs: Math.min(previousMeta?.sourceFirstSeenAtMs ?? record.firstSeenAtMs, record.firstSeenAtMs),
        sourceLastSeenAtMs: Math.max(previousMeta?.sourceLastSeenAtMs ?? 0, record.lastSeenAtMs),
        surfaces: [...new Set([...(previousMeta?.surfaces ?? []), ...record.surfaces])].sort(),
        processedEvidenceIds: [...new Set([...(previousMeta?.processedEvidenceIds ?? []), ...unseen.map(row => row.evidenceId)])].slice(-8),
        feedOnlyAtFirstIngestion: previousMeta?.feedOnlyAtFirstIngestion ?? !wasKnown,
        newlySelectorQualified: previousMeta?.newlySelectorQualified ?? false,
      };
      const latest = unseen[unseen.length - 1];
      const existing = this.state.portfolios[record.portfolioId];
      const consistent = !existing || ((!existing.ownerId || !latest.ownerId || existing.ownerId === latest.ownerId)
        && (!existing.username || !latest.username || existing.username === latest.username));
      if (!consistent) meta.lifetime.rejectedIdentityConflicts += 1;
      // Feed evidence is additive. It must never refresh, overwrite, or demote an
      // existing canonical verified candidate. Feed-only rows may enter the selector
      // only when the feed itself carries internally consistent verified profile data.
      const feedVerified = latest.verified === true && Boolean(latest.ownerId && latest.username);
      if (consistent && existing) {
        // Existing broad/profile state remains authoritative; retain its original
        // observation timestamp and source so social activity cannot manufacture
        // freshness for stale selector economics.
      } else if (consistent && feedVerified) {
        const raw = {
          ...latest.profile,
          id: record.portfolioId,
          ownerId: latest.ownerId,
          owner: { id: latest.ownerId, username: latest.username, verified: true },
        };
        const candidate = classifyPortfolio(raw, meta.records[record.portfolioId].firstProcessedAtMs, `feed:${latest.surface}`, this.policy);
        if (candidate?.reasons.includes('incomplete_profile_metrics') || candidate?.reasons.includes('inconsistent_profile_metrics')) {
          meta.lifetime.rejectedMalformed += 1;
        } else {
          if (candidate && this.storeCanonical(candidate)) {
            this.recentRows.push(candidate);
            this.appendBoundedSnapshot(candidate);
          }
        }
      } else if (consistent) {
        meta.lifetime.rejectedUnverified += 1;
      }
      observationsProcessed += unseen.length;
      if (!wasElite && this.state.portfolios[record.portfolioId]?.bucket === 'ELITE_CANDIDATE') {
        if (!meta.records[record.portfolioId].newlySelectorQualified) meta.lifetime.newlyQualified += 1;
        meta.records[record.portfolioId].newlySelectorQualified = true;
      }
    }
    const retained = Object.entries(meta.records).sort((a, b) => b[1].lastProcessedAtMs - a[1].lastProcessedAtMs || a[0].localeCompare(b[0])).slice(0, FEED_SELECTOR_TRACKING_MAX_PORTFOLIOS);
    meta.records = Object.fromEntries(retained);
    this.state.feedEvidence = meta;
    if (observationsProcessed || initializedEligibilityBoundary) this.saveState();
    return { observationsProcessed, ...this.feedExpansionReport(processedAtMs) };
  }

  feedExpansionReport(nowMs = Date.now()) {
    const meta = this.state.feedEvidence ?? emptyFeedEvidence();
    const rows = Object.entries(meta.records).map(([portfolioId, record]) => ({
      portfolioId,
      surfaces: record.surfaces, firstSeenAtMs: record.sourceFirstSeenAtMs, lastSeenAtMs: record.sourceLastSeenAtMs,
      firstProcessedAtMs: record.firstProcessedAtMs, lastProcessedAtMs: record.lastProcessedAtMs,
      ingestionLagMs: Math.max(0, record.firstProcessedAtMs - record.sourceFirstSeenAtMs),
      feedOnlyAtFirstIngestion: record.feedOnlyAtFirstIngestion, newlySelectorQualified: record.newlySelectorQualified,
      currentBucket: this.state.portfolios[portfolioId]?.bucket ?? null,
    }));
    return {
      measuredAtMs: nowMs,
      totalFeedDiscoveredUniquePortfolios: meta.lifetime.discovered,
      newVsBroadDiscovery: meta.lifetime.feedOnly,
      newlySelectorQualified: meta.lifetime.newlyQualified,
      retainedTrackingRecords: rows.length, trackingRecordCap: FEED_SELECTOR_TRACKING_MAX_PORTFOLIOS,
      rejectedUnverified: meta.lifetime.rejectedUnverified, rejectedMalformed: meta.lifetime.rejectedMalformed,
      rejectedIdentityConflicts: meta.lifetime.rejectedIdentityConflicts, dedupedReplayCount: meta.lifetime.dedupedReplayCount,
      hydrationQueuePortfolioIds: rows.filter(row => row.currentBucket == null).map(row => row.portfolioId),
      hydrationSource: '/v1_0/trending/get_portfolios_pl broad/profile cycle',
      portfolios: rows,
    };
  }

  private saveState() {
    if (Object.keys(this.state.portfolios).length > CANDIDATE_STATE_MAX_PORTFOLIOS) {
      throw new Error('candidate state portfolio cap exceeded');
    }
    const serialized = JSON.stringify(this.state, null, 2);
    if (Buffer.byteLength(serialized) > CANDIDATE_STATE_MAX_BYTES) {
      throw new Error('candidate state byte cap exceeded');
    }
    const tmp = `${this.statePath}.tmp`;
    writeFileSync(tmp, serialized);
    renameSync(tmp, this.statePath);
  }

  private storeCanonical(snapshot: PortfolioSnapshot) {
    const previous = this.state.portfolios[snapshot.portfolioId];
    if (!previous && Object.keys(this.state.portfolios).length >= CANDIDATE_STATE_MAX_PORTFOLIOS) return false;
    const tentativePortfolios = { ...this.state.portfolios, [snapshot.portfolioId]: snapshot };
    const tentativeFirstEliteAtMs = { ...this.state.firstEliteAtMs };
    if (snapshot.bucket === 'ELITE_CANDIDATE' && tentativeFirstEliteAtMs[snapshot.portfolioId] == null) {
      tentativeFirstEliteAtMs[snapshot.portfolioId] = snapshot.observedAtMs;
    }
    const tentativeState = {
      ...this.state,
      portfolios: tentativePortfolios,
      firstEliteAtMs: tentativeFirstEliteAtMs,
    };
    if (Buffer.byteLength(JSON.stringify(tentativeState)) > CANDIDATE_STATE_MAX_BYTES) return false;
    this.state.portfolios = tentativePortfolios;
    this.state.firstEliteAtMs = tentativeFirstEliteAtMs;
    return true;
  }

  private archivePreviousSnapshot() {
    const previous = `${this.snapshotsPath}.previous`;
    if (!existsSync(previous)) return;
    const archiveDir = `${this.snapshotsPath}.archive`;
    mkdirSync(archiveDir, { recursive: true });
    let suffix = Math.max(1, Math.trunc(statSync(previous).mtimeMs));
    let destination = `${archiveDir}/segment-${suffix}.jsonl`;
    while (existsSync(destination)) {
      suffix += 1;
      destination = `${archiveDir}/segment-${suffix}.jsonl`;
    }
    renameSync(previous, destination);
  }

  private appendBoundedSnapshot(snapshot: PortfolioSnapshot) {
    const line = `${JSON.stringify(snapshot)}
`;
    const lineBytes = Buffer.byteLength(line);
    if (lineBytes > CANDIDATE_SNAPSHOT_SEGMENT_MAX_BYTES) {
      throw new Error('candidate snapshot exceeds segment byte cap');
    }
    const previous = `${this.snapshotsPath}.previous`;
    const currentBytes = existsSync(this.snapshotsPath) ? statSync(this.snapshotsPath).size : 0;
    if (currentBytes + lineBytes > CANDIDATE_SNAPSHOT_SEGMENT_MAX_BYTES) {
      this.archivePreviousSnapshot();
      if (existsSync(this.snapshotsPath)) renameSync(this.snapshotsPath, previous);
    }
    appendFileSync(this.snapshotsPath, line);
    const hotBytes = (existsSync(this.snapshotsPath) ? statSync(this.snapshotsPath).size : 0)
      + (existsSync(previous) ? statSync(previous).size : 0);
    if (hotBytes > CANDIDATE_SNAPSHOT_TOTAL_MAX_BYTES) {
      throw new Error('candidate hot snapshot journal byte cap exceeded');
    }
  }

  observe(items: any[], sourceFilter: string, observedAtMs = Date.now()) {
    mkdirSync(dirname(this.statePath), { recursive: true });
    mkdirSync(dirname(this.snapshotsPath), { recursive: true });
    const snapshots: PortfolioSnapshot[] = [];
    for (const raw of items) {
      const snapshot = classifyPortfolio(raw, observedAtMs, sourceFilter, this.policy);
      if (!snapshot) continue;
      snapshots.push(snapshot);
      const previous = this.state.portfolios[snapshot.portfolioId];
      if (!previous || snapshot.observedAtMs > previous.observedAtMs
        || (snapshot.observedAtMs === previous.observedAtMs
          && previous.bucket === 'ELITE_CANDIDATE' && snapshot.bucket !== 'ELITE_CANDIDATE')) {
        if (!this.storeCanonical(snapshot)) continue;
      }
      this.appendBoundedSnapshot(snapshot);
      this.recentRows.push(snapshot);
    }
    this.state.lastObservedAtMs = Math.max(this.state.lastObservedAtMs, observedAtMs);
    this.saveState();
    // The executor reads only this bounded causal index. Three observations cover
    // the 25s signal window across a 10-minute collector boundary; the 30-minute
    // time retention also bounds memory independently of append-only history age.
    const cutoff = observedAtMs - 30 * 60_000;
    const grouped = new Map<string, Map<number, PortfolioSnapshot>>();
    for (const row of this.recentRows) {
      if (!validRecentSnapshot(row) || !(row.observedAtMs >= cutoff)) continue;
      const observations = grouped.get(row.portfolioId) ?? new Map<number, PortfolioSnapshot>();
      const prior = observations.get(row.observedAtMs);
      // A cycle can return the same portfolio from several bounded surfaces. One
      // timestamp is one observation, and disagreement fails closed: non-elite wins.
      if (!prior || (prior.bucket === 'ELITE_CANDIDATE' && row.bucket !== 'ELITE_CANDIDATE')) {
        observations.set(row.observedAtMs, row);
      }
      grouped.set(row.portfolioId, observations);
    }
    this.recentRows = [...grouped.values()].flatMap(observations => [...observations.values()]
      .sort((a, b) => b.observedAtMs - a.observedAtMs).slice(0, 3)).sort((a, b) => a.observedAtMs - b.observedAtMs);
    const recentPath = `${this.snapshotsPath}.recent.json`;
    const recentTmp = `${recentPath}.tmp`;
    writeFileSync(recentTmp, JSON.stringify({
      version: 1,
      selectorVersion: ELITE_SELECTOR_VERSION,
      generatedAtMs: observedAtMs,
      rows: this.recentRows,
    }));
    renameSync(recentTmp, recentPath);
    return snapshots;
  }

  get(portfolioId: string): PortfolioSnapshot | null {
    return this.state.portfolios[portfolioId] ?? null;
  }

  isEliteAtLatest(portfolioId: string): boolean {
    return this.get(portfolioId)?.bucket === 'ELITE_CANDIDATE';
  }

  firstEliteAtMs(portfolioId: string): number | null {
    return this.state.firstEliteAtMs[portfolioId] ?? null;
  }

  report() {
    const portfolios = Object.values(this.state.portfolios).sort((a, b) => b.score - a.score || a.portfolioId.localeCompare(b.portfolioId));
    const count = (bucket: PortfolioBucket) => portfolios.filter(p => p.bucket === bucket).length;
    const ownerIds = new Set(portfolios.map(p => p.ownerId).filter((v): v is string => Boolean(v)));
    return {
      selectorVersion: ELITE_SELECTOR_VERSION,
      policy: this.policy,
      lastObservedAtMs: this.state.lastObservedAtMs,
      uniquePortfolioCount: portfolios.length,
      uniqueOwnerCount: ownerIds.size,
      buckets: {
        ELITE_CANDIDATE: count('ELITE_CANDIDATE'),
        SPARSE_HIGH_RETURN: count('SPARSE_HIGH_RETURN'),
        RESEARCH_WIDE: count('RESEARCH_WIDE'),
        REJECTED_DEMOTED: count('REJECTED_DEMOTED'),
      },
      elitePortfolioIds: portfolios.filter(p => p.bucket === 'ELITE_CANDIDATE').map(p => p.portfolioId),
      portfolios,
    };
  }
}
