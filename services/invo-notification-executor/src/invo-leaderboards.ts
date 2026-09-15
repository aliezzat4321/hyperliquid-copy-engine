import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs';
import { dirname } from 'path';

export const INVO_LEADERBOARD_VERSION = 'invo-top-portfolios-horizons-v1-20260916';
export const CANONICAL_INVO_HORIZONS = ['1D', '1W', '1M', '1Y', 'AT'] as const;
export type InvoLeaderboardHorizon = typeof CANONICAL_INVO_HORIZONS[number];

export interface InvoLeaderboardSnapshot {
  observedAtMs: number;
  version: string;
  sourceEndpoint: '/v1_0/trending/get_portfolios_pl';
  horizon: InvoLeaderboardHorizon;
  rank: number;
  portfolioId: string;
  portfolioName: string | null;
  ownerId: string | null;
  username: string | null;
  verified: boolean | null;
  percentChange: number | null;
  pnlUnit: string | null;
  plSnapshot: unknown;
  changeInPl: number | null;
  avgPlRealized: number | null;
  openPositions: number | null;
  closedPositions: number | null;
  wonPositions: number | null;
  lostPositions: number | null;
  winRatePct: number | null;
  liquidated: boolean | null;
  createdAtMs: number | null;
  updatedAtMs: number | null;
  avgHoldTimeSeconds: number | null;
  directionBias: unknown;
  topPosition: unknown;
  rawShapeKeys: string[];
}

interface LeaderboardDiskState {
  version: 1;
  leaderboardVersion: string;
  sourceEndpoint: '/v1_0/trending/get_portfolios_pl';
  latestByHorizon: Partial<Record<InvoLeaderboardHorizon, InvoLeaderboardSnapshot[]>>;
  lastObservedAtMs: number;
}

function finite(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function bool(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

function timestamp(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value < 10_000_000_000 ? value * 1000 : value;
  if (typeof value !== 'string' || !value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function normalizeLeaderboardHorizon(value: string): InvoLeaderboardHorizon | null {
  const normalized = value.trim().toUpperCase();
  return (CANONICAL_INVO_HORIZONS as readonly string[]).includes(normalized)
    ? normalized as InvoLeaderboardHorizon
    : null;
}

export function parseLeaderboardItem(
  raw: any,
  horizon: InvoLeaderboardHorizon,
  rank: number,
  observedAtMs: number,
): InvoLeaderboardSnapshot | null {
  const portfolioId = String(raw?.id ?? raw?.portfolioId ?? raw?._id ?? '').trim();
  if (!portfolioId || !Number.isInteger(rank) || rank < 1) return null;
  const owner = raw?.owner ?? raw?.user ?? {};
  return {
    observedAtMs,
    version: INVO_LEADERBOARD_VERSION,
    sourceEndpoint: '/v1_0/trending/get_portfolios_pl',
    horizon,
    rank,
    portfolioId,
    portfolioName: text(raw?.title ?? raw?.name ?? raw?.portfolioName),
    ownerId: text(raw?.ownerId ?? owner?.id),
    username: text(owner?.username ?? raw?.username)?.replace(/^@/, '').toLowerCase() ?? null,
    verified: bool(owner?.verified ?? raw?.verified ?? raw?.isVerified),
    percentChange: finite(raw?.percentChange ?? raw?.pnlPercent ?? raw?.profitLossPercent ?? raw?.roi),
    pnlUnit: text(raw?.pnlUnit),
    plSnapshot: raw?.plSnapshot ?? null,
    changeInPl: finite(raw?.changeInPl),
    avgPlRealized: finite(raw?.avgPlRealized),
    openPositions: finite(raw?.openPositions ?? raw?.openTrades),
    closedPositions: finite(raw?.closedPositions ?? raw?.closedTrades ?? raw?.totalClosedPositions),
    wonPositions: finite(raw?.wonPositions ?? raw?.winningPositions ?? raw?.wins),
    lostPositions: finite(raw?.lostPositions ?? raw?.losingPositions ?? raw?.losses),
    winRatePct: finite(raw?.winRate ?? raw?.win_rate ?? raw?.winRatePct),
    liquidated: bool(raw?.liquidated ?? raw?.isLiquidated),
    createdAtMs: timestamp(raw?.createdAt ?? raw?.created_at),
    updatedAtMs: timestamp(raw?.updatedAt ?? raw?.updated_at),
    avgHoldTimeSeconds: finite(raw?.avgHoldTimeSeconds),
    directionBias: raw?.directionBias ?? null,
    topPosition: raw?.topPosition ?? null,
    rawShapeKeys: Object.keys(raw ?? {}).sort(),
  };
}

export class InvoLeaderboardLedger {
  private state: LeaderboardDiskState;

  constructor(private readonly statePath: string, private readonly snapshotsPath: string) {
    this.state = {
      version: 1,
      leaderboardVersion: INVO_LEADERBOARD_VERSION,
      sourceEndpoint: '/v1_0/trending/get_portfolios_pl',
      latestByHorizon: {},
      lastObservedAtMs: 0,
    };
    if (existsSync(statePath)) {
      try {
        const parsed = JSON.parse(readFileSync(statePath, 'utf8')) as Partial<LeaderboardDiskState>;
        this.state = {
          version: 1,
          leaderboardVersion: INVO_LEADERBOARD_VERSION,
          sourceEndpoint: '/v1_0/trending/get_portfolios_pl',
          latestByHorizon: parsed.latestByHorizon ?? {},
          lastObservedAtMs: parsed.lastObservedAtMs ?? 0,
        };
      } catch {
        // Corrupt derived leaderboard state must not stop research; immutable snapshots remain authoritative.
      }
    }
  }

  observePage(
    items: any[],
    horizon: InvoLeaderboardHorizon,
    observedAtMs = Date.now(),
    rankOffset = 0,
  ): InvoLeaderboardSnapshot[] {
    mkdirSync(dirname(this.statePath), { recursive: true });
    mkdirSync(dirname(this.snapshotsPath), { recursive: true });
    const parsed = items
      .map((raw, index) => parseLeaderboardItem(raw, horizon, rankOffset + index + 1, observedAtMs))
      .filter((row): row is InvoLeaderboardSnapshot => Boolean(row));
    for (const row of parsed) appendFileSync(this.snapshotsPath, `${JSON.stringify(row)}\n`);

    const existing = this.state.latestByHorizon[horizon] ?? [];
    const byRank = new Map(existing.map(row => [row.rank, row]));
    // A new observation timestamp starts a fresh current leaderboard for the horizon.
    if (existing.length && existing[0]?.observedAtMs !== observedAtMs && rankOffset === 0) byRank.clear();
    for (const row of parsed) byRank.set(row.rank, row);
    this.state.latestByHorizon[horizon] = [...byRank.values()].sort((a, b) => a.rank - b.rank);
    this.state.lastObservedAtMs = Math.max(this.state.lastObservedAtMs, observedAtMs);
    this.save();
    return parsed;
  }

  private save() {
    const temp = `${this.statePath}.tmp`;
    writeFileSync(temp, JSON.stringify(this.state, null, 2));
    renameSync(temp, this.statePath);
  }

  report() {
    const latestByHorizon = Object.fromEntries(
      CANONICAL_INVO_HORIZONS.map(horizon => [horizon, this.state.latestByHorizon[horizon] ?? []]),
    ) as Record<InvoLeaderboardHorizon, InvoLeaderboardSnapshot[]>;
    const appearances = new Map<string, {
      portfolioId: string;
      portfolioName: string | null;
      ownerId: string | null;
      username: string | null;
      horizons: InvoLeaderboardHorizon[];
      ranks: Partial<Record<InvoLeaderboardHorizon, number>>;
      returns: Partial<Record<InvoLeaderboardHorizon, number | null>>;
    }>();
    for (const horizon of CANONICAL_INVO_HORIZONS) {
      for (const row of latestByHorizon[horizon]) {
        const current = appearances.get(row.portfolioId) ?? {
          portfolioId: row.portfolioId,
          portfolioName: row.portfolioName,
          ownerId: row.ownerId,
          username: row.username,
          horizons: [],
          ranks: {},
          returns: {},
        };
        if (!current.horizons.includes(horizon)) current.horizons.push(horizon);
        current.ranks[horizon] = row.rank;
        current.returns[horizon] = row.percentChange;
        appearances.set(row.portfolioId, current);
      }
    }
    const crossHorizon = [...appearances.values()]
      .map(row => ({ ...row, appearanceCount: row.horizons.length }))
      .sort((a, b) => b.appearanceCount - a.appearanceCount || Math.min(...Object.values(a.ranks).map(Number)) - Math.min(...Object.values(b.ranks).map(Number)) || a.portfolioId.localeCompare(b.portfolioId));
    const ownerIds = new Set(crossHorizon.map(row => row.ownerId).filter((value): value is string => Boolean(value)));
    return {
      leaderboardVersion: INVO_LEADERBOARD_VERSION,
      sourceEndpoint: this.state.sourceEndpoint,
      lastObservedAtMs: this.state.lastObservedAtMs,
      horizons: CANONICAL_INVO_HORIZONS,
      countsByHorizon: Object.fromEntries(CANONICAL_INVO_HORIZONS.map(h => [h, latestByHorizon[h].length])),
      uniquePortfolioCount: appearances.size,
      uniqueOwnerCount: ownerIds.size,
      latestByHorizon,
      crossHorizon,
    };
  }
}
