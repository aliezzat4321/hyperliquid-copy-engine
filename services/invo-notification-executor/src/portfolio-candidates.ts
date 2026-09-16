import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs';
import { dirname } from 'path';

export const ELITE_SELECTOR_VERSION = 'invo-portfolio-elite-v2-20260916';

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
  // Shadow research should be able to discover strong newer traders. These are
  // admission floors, not targets: confidence continues to rise above them.
  minClosedPositions: 20,
  minDaysActive: 7,
  minWinRatePct: 80,
  minPercentChange: 0.01,
  minQualityScore: 60,
  sparseMinClosedPositions: 8,
  sparseMinWinRatePct: 60,
  sparseMinPercentChange: 100,
});

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

interface LedgerDiskState {
  version: 1;
  selectorVersion: string;
  policy: PortfolioSelectorPolicy;
  portfolios: Record<string, PortfolioSnapshot>;
  firstEliteAtMs: Record<string, number>;
  lastObservedAtMs: number;
}

function finite(v: unknown): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function integer(v: unknown, fallback = 0): number {
  const n = finite(v);
  return n == null ? fallback : Math.max(0, Math.trunc(n));
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

function scorePortfolio(input: {
  closedPositions: number;
  daysActive: number | null;
  winRatePct: number | null;
  percentChange: number | null;
  recentActivityDaysAgo: number | null;
}): { score: number; breakdown: PortfolioScoreBreakdown; closedPositionsPerDay: number | null } {
  const closedPositionsPerDay = input.daysActive != null && input.daysActive > 0
    ? input.closedPositions / Math.max(input.daysActive, 1)
    : null;

  // 30 points: only 80%+ can enter elite shadow. Within that high-quality band,
  // 85/90/95% progressively earn more credit rather than treating all strong win rates equally.
  const winRate = input.winRatePct == null
    ? 0
    : input.winRatePct < 80
      ? linearPoints(input.winRatePct, 50, 80, 20)
      : 20 + linearPoints(input.winRatePct, 80, 95, 10);

  // 30 points: historical return, deliberately saturating. 500% is excellent and
  // receives near-maximum credit, but it is not a minimum admission threshold.
  const historicalReturn = input.percentChange == null || input.percentChange <= 0
    ? 0
    : clamp(Math.log1p(input.percentChange) / Math.log1p(1000), 0, 1) * 30;

  // 15 points: 20 trades can qualify; 30+ earns more confidence, 100+ approaches max.
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
  const closedPositions = integer(raw?.closedPositions ?? raw?.closedTrades ?? raw?.totalClosedPositions);
  const openPositions = finite(raw?.openPositions ?? raw?.openTrades);
  const wonPositions = integer(raw?.wonPositions ?? raw?.winningPositions ?? raw?.wins);
  const lostPositions = integer(raw?.lostPositions ?? raw?.losingPositions ?? raw?.losses);
  const winRatePct = finite(raw?.winRate ?? raw?.win_rate ?? raw?.winRatePct);
  const percentChange = finite(raw?.percentChange ?? raw?.pnlPercent ?? raw?.profitLossPercent ?? raw?.roi);
  const liquidated = bool(raw?.liquidated ?? raw?.isLiquidated);
  const verified = bool(owner?.verified ?? raw?.verified ?? raw?.isVerified);
  const currentWinStreak = finite(raw?.currentWinStreak ?? raw?.winStreak);
  const followerCount = finite(raw?.followerCount ?? raw?.followers);
  // This is a count ratio, not an average-win / average-loss payout ratio. Keep it
  // for observability, but do not double-count it as independent profitability evidence.
  const winLossRatio = lostPositions > 0 ? wonPositions / lostPositions : wonPositions > 0 ? wonPositions : null;

  const scored = scorePortfolio({
    closedPositions,
    daysActive,
    winRatePct,
    percentChange,
    recentActivityDaysAgo,
  });

  const reasons: string[] = [];
  let bucket: PortfolioBucket;

  const hardReject = liquidated === true
    || (closedPositions >= policy.minClosedPositions && winRatePct != null && winRatePct < 40)
    || (closedPositions >= policy.minClosedPositions && percentChange != null && percentChange < 0);
  const enoughAge = daysActive == null || daysActive >= policy.minDaysActive;
  const enoughSample = closedPositions >= policy.minClosedPositions;
  const positiveReturn = percentChange != null && percentChange >= policy.minPercentChange;
  const enoughWinRate = winRatePct != null && winRatePct >= policy.minWinRatePct;
  const elite = !hardReject
    && enoughSample
    && enoughAge
    && enoughWinRate
    && positiveReturn
    && scored.score >= policy.minQualityScore;

  if (elite) {
    bucket = 'ELITE_CANDIDATE';
    reasons.push('meets_weighted_quality_gate_v2');
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
    if (!enoughSample) reasons.push('below_20_closed_sample');
    if (!enoughAge) reasons.push('below_7_active_days');
    if (winRatePct == null) reasons.push('missing_win_rate');
    else if (!enoughWinRate) reasons.push('win_rate_below_floor');
    if (percentChange == null) reasons.push('missing_return');
    else if (!positiveReturn) reasons.push('return_not_positive');
    if (scored.score < policy.minQualityScore) reasons.push('weighted_quality_score_below_gate');
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
    };
    if (existsSync(statePath)) {
      try {
        const parsed = JSON.parse(readFileSync(statePath, 'utf8')) as Partial<LedgerDiskState>;
        this.state = {
          version: 1,
          selectorVersion: ELITE_SELECTOR_VERSION,
          policy,
          portfolios: parsed.portfolios ?? {},
          firstEliteAtMs: parsed.selectorVersion === ELITE_SELECTOR_VERSION ? (parsed.firstEliteAtMs ?? {}) : {},
          lastObservedAtMs: parsed.lastObservedAtMs ?? 0,
        };
      } catch {
        // A malformed prior candidate file must not stop broad research; start a clean candidate view.
      }
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
      if (!previous || snapshot.observedAtMs >= previous.observedAtMs) this.state.portfolios[snapshot.portfolioId] = snapshot;
      if (snapshot.bucket === 'ELITE_CANDIDATE' && this.state.firstEliteAtMs[snapshot.portfolioId] == null) {
        this.state.firstEliteAtMs[snapshot.portfolioId] = observedAtMs;
      }
      appendFileSync(this.snapshotsPath, `${JSON.stringify(snapshot)}\n`);
    }
    this.state.lastObservedAtMs = Math.max(this.state.lastObservedAtMs, observedAtMs);
    const tmp = `${this.statePath}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.state, null, 2));
    renameSync(tmp, this.statePath);
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