import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs';
import { dirname } from 'path';

export const ELITE_SELECTOR_VERSION = 'invo-portfolio-elite-v1-20260916';

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
  minWinLossRatio: number;
  sparseMinWinRatePct: number;
  sparseMinPercentChange: number;
}

export const DEFAULT_PORTFOLIO_SELECTOR: PortfolioSelectorPolicy = Object.freeze({
  minClosedPositions: 100,
  minDaysActive: 90,
  minWinRatePct: 80,
  minPercentChange: 500,
  minWinLossRatio: 3,
  sparseMinWinRatePct: 80,
  sparseMinPercentChange: 500,
});

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
  openPositions: number | null;
  closedPositions: number;
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
  const winLossRatio = lostPositions > 0 ? wonPositions / lostPositions : wonPositions > 0 ? wonPositions : null;

  const reasons: string[] = [];
  let bucket: PortfolioBucket;

  const hardReject = liquidated === true || (closedPositions >= 30 && winRatePct != null && winRatePct < 45) || (closedPositions >= 30 && percentChange != null && percentChange < 0);
  const enoughAge = daysActive == null || daysActive >= policy.minDaysActive;
  const elite = !hardReject
    && closedPositions >= policy.minClosedPositions
    && enoughAge
    && winRatePct != null && winRatePct >= policy.minWinRatePct
    && percentChange != null && percentChange >= policy.minPercentChange
    && winLossRatio != null && winLossRatio >= policy.minWinLossRatio;

  if (elite) {
    bucket = 'ELITE_CANDIDATE';
    reasons.push('meets_frozen_quality_gate');
  } else if (!hardReject && closedPositions < policy.minClosedPositions && winRatePct != null && winRatePct >= policy.sparseMinWinRatePct && percentChange != null && percentChange >= policy.sparseMinPercentChange) {
    bucket = 'SPARSE_HIGH_RETURN';
    reasons.push('high_return_but_insufficient_closed_sample');
  } else if (hardReject) {
    bucket = 'REJECTED_DEMOTED';
    if (liquidated === true) reasons.push('liquidated');
    if (closedPositions >= 30 && winRatePct != null && winRatePct < 45) reasons.push('low_win_rate');
    if (closedPositions >= 30 && percentChange != null && percentChange < 0) reasons.push('negative_historical_return');
  } else {
    bucket = 'RESEARCH_WIDE';
    if (closedPositions < policy.minClosedPositions) reasons.push('insufficient_closed_sample');
    if (!enoughAge) reasons.push('insufficient_age');
    if (winRatePct == null) reasons.push('missing_win_rate');
    else if (winRatePct < policy.minWinRatePct) reasons.push('win_rate_below_elite_gate');
    if (percentChange == null) reasons.push('missing_return');
    else if (percentChange < policy.minPercentChange) reasons.push('return_below_elite_gate');
    if (winLossRatio == null) reasons.push('missing_win_loss_ratio');
    else if (winLossRatio < policy.minWinLossRatio) reasons.push('win_loss_ratio_below_elite_gate');
  }

  const sampleScore = Math.log1p(closedPositions) * 20;
  const winRateScore = (winRatePct ?? 0) * 2;
  const pnlScore = Math.max(-1000, Math.min(percentChange ?? 0, 10_000)) * 0.01;
  const wlScore = Math.min(winLossRatio ?? 0, 20) * 10;
  const streakScore = Math.min(currentWinStreak ?? 0, 50);
  const liquidationPenalty = liquidated === true ? 1000 : 0;
  const score = Math.round((sampleScore + winRateScore + pnlScore + wlScore + streakScore - liquidationPenalty) * 100) / 100;

  return {
    observedAtMs,
    sourceFilter,
    portfolioId,
    portfolioName,
    ownerId,
    username,
    verified,
    createdAtMs,
    daysActive: daysActive == null ? null : Math.round(daysActive * 100) / 100,
    openPositions: openPositions == null ? null : Math.max(0, Math.trunc(openPositions)),
    closedPositions,
    wonPositions,
    lostPositions,
    winRatePct,
    percentChange,
    winLossRatio: winLossRatio == null ? null : Math.round(winLossRatio * 1000) / 1000,
    currentWinStreak,
    followerCount,
    liquidated,
    bucket,
    score,
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
          firstEliteAtMs: parsed.firstEliteAtMs ?? {},
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
