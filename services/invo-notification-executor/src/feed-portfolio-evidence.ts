import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from 'fs';
import { createHash } from 'crypto';
import { dirname } from 'path';
import type { InvoFeedSurface } from './feed-surfaces.js';

export const FEED_PORTFOLIO_EVIDENCE_VERSION = 'lane3-feed-portfolio-evidence-v1-20260920';
const MAX_PORTFOLIOS = 5_000;
const MAX_OBSERVATIONS_PER_PORTFOLIO = 8;
const MAX_JOURNAL_BYTES = 2_000_000;

export interface FeedPortfolioObservation {
  evidenceId: string;
  surface: InvoFeedSurface;
  postId: string | null;
  sourceTradeAtMs: number | null;
  capturedAtMs: number;
  portfolioId: string;
  ownerId: string | null;
  username: string | null;
  portfolioCreatedAtMs: number | null;
  rawPortfolio: Record<string, unknown>;
  rawPortfolioShapeKeys: string[];
  rawPostShapeKeys: string[];
  rawUpdateShapeKeys: string[];
}

export interface FeedPortfolioRecord {
  portfolioId: string;
  firstSeenAtMs: number;
  lastSeenAtMs: number;
  surfaces: InvoFeedSurface[];
  ownerIds: string[];
  usernames: string[];
  observations: FeedPortfolioObservation[];
}

interface FeedEvidenceState {
  version: typeof FEED_PORTFOLIO_EVIDENCE_VERSION;
  generatedAtMs: number;
  portfolios: Record<string, FeedPortfolioRecord>;
}

function timestamp(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value < 10_000_000_000 ? value * 1000 : value;
  if (typeof value !== 'string' || !value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function object(value: unknown): Record<string, unknown> | null {
  return value != null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

/** Extract only portfolio evidence actually present in a feed row. No endpoint hydration or guessed fields. */
export function normalizeFeedPortfolioObservation(
  post: any,
  surface: InvoFeedSurface,
  capturedAtMs: number,
): FeedPortfolioObservation | null {
  const update = object(post?.update) ?? object(post?.investment) ?? object(post?.data) ?? {};
  const portfolio = object(update.portfolio) ?? object(post?.portfolio);
  if (!portfolio) return null;
  const serializedPortfolio = JSON.stringify(portfolio);
  if (Buffer.byteLength(serializedPortfolio, 'utf8') > 65_536) return null;
  const boundedPortfolio = JSON.parse(serializedPortfolio) as Record<string, unknown>;
  const portfolioId = text(portfolio.id ?? portfolio.portfolioId ?? portfolio._id);
  if (!portfolioId) return null;
  const owner = object(update.owner) ?? object(post?.owner) ?? object(portfolio.owner) ?? object(portfolio.user) ?? {};
  const ownerId = text(update.ownerId ?? post?.ownerId ?? portfolio.ownerId ?? owner.id);
  const username = text(owner.username ?? update.username ?? post?.username ?? portfolio.username)
    ?.replace(/^@/, '').toLowerCase() ?? null;
  const sourceTradeAtMs = timestamp(
    update.closedAt ?? update.updatedAt ?? update.createdAt ?? post?.updatedAt ?? post?.createdAt,
  );
  const portfolioCreatedAtMs = timestamp(portfolio.createdAt ?? portfolio.created_at);
  const postId = text(post?.id ?? post?._id);
  const identity = JSON.stringify([surface, postId, portfolioId, sourceTradeAtMs, serializedPortfolio]);
  return {
    evidenceId: createHash('sha256').update(identity).digest('hex'),
    surface,
    postId,
    sourceTradeAtMs,
    capturedAtMs,
    portfolioId,
    ownerId,
    username,
    portfolioCreatedAtMs,
    rawPortfolio: boundedPortfolio,
    rawPortfolioShapeKeys: Object.keys(boundedPortfolio).sort(),
    rawPostShapeKeys: Object.keys(object(post) ?? {}).sort(),
    rawUpdateShapeKeys: Object.keys(update).sort(),
  };
}

function emptyState(): FeedEvidenceState {
  return { version: FEED_PORTFOLIO_EVIDENCE_VERSION, generatedAtMs: 0, portfolios: {} };
}

function applyObservation(state: FeedEvidenceState, row: FeedPortfolioObservation) {
  const prior = state.portfolios[row.portfolioId];
  const observations = prior?.observations ?? [];
  if (observations.some(item => item.evidenceId === row.evidenceId)) return false;
  const merged = [...observations, row]
    .sort((a, b) => a.capturedAtMs - b.capturedAtMs || a.evidenceId.localeCompare(b.evidenceId))
    .slice(-MAX_OBSERVATIONS_PER_PORTFOLIO);
  state.portfolios[row.portfolioId] = {
    portfolioId: row.portfolioId,
    firstSeenAtMs: Math.min(prior?.firstSeenAtMs ?? row.capturedAtMs, row.capturedAtMs),
    lastSeenAtMs: Math.max(prior?.lastSeenAtMs ?? 0, row.capturedAtMs),
    surfaces: [...new Set([...(prior?.surfaces ?? []), row.surface])].sort() as InvoFeedSurface[],
    ownerIds: [...new Set([...(prior?.ownerIds ?? []), ...(row.ownerId ? [row.ownerId] : [])])].sort(),
    usernames: [...new Set([...(prior?.usernames ?? []), ...(row.username ? [row.username] : [])])].sort(),
    observations: merged,
  };
  state.generatedAtMs = Math.max(state.generatedAtMs, row.capturedAtMs);
  return true;
}

export function loadFeedPortfolioEvidence(path: string): FeedEvidenceState {
  let state = emptyState();
  if (existsSync(path)) {
    try {
      const parsed = JSON.parse(readFileSync(path, 'utf8')) as FeedEvidenceState;
      if (parsed.version === FEED_PORTFOLIO_EVIDENCE_VERSION && parsed.portfolios) state = parsed;
    } catch { /* journal replay below repairs an interrupted snapshot write */ }
  }
  const journal = `${path}.journal.jsonl`;
  if (existsSync(journal)) {
    for (const line of readFileSync(journal, 'utf8').split('\n')) {
      if (!line.trim()) continue;
      try { applyObservation(state, JSON.parse(line) as FeedPortfolioObservation); } catch { /* fail closed per row */ }
    }
  }
  return state;
}

export class FeedPortfolioEvidenceStore {
  private state: FeedEvidenceState;

  constructor(private readonly path: string) {
    this.state = loadFeedPortfolioEvidence(path);
  }

  observe(posts: any[], surface: InvoFeedSurface, capturedAtMs = Date.now()): FeedPortfolioObservation[] {
    mkdirSync(dirname(this.path), { recursive: true });
    const accepted: FeedPortfolioObservation[] = [];
    for (const post of posts) {
      const row = normalizeFeedPortfolioObservation(post, surface, capturedAtMs);
      if (!row || !applyObservation(this.state, row)) continue;
      appendFileSync(`${this.path}.journal.jsonl`, `${JSON.stringify(row)}\n`);
      accepted.push(row);
    }
    const retained = Object.values(this.state.portfolios)
      .sort((a, b) => b.lastSeenAtMs - a.lastSeenAtMs || a.portfolioId.localeCompare(b.portfolioId))
      .slice(0, MAX_PORTFOLIOS);
    this.state.portfolios = Object.fromEntries(retained.map(row => [row.portfolioId, row]));
    this.save();
    if (existsSync(`${this.path}.journal.jsonl`) && statSync(`${this.path}.journal.jsonl`).size > MAX_JOURNAL_BYTES) {
      writeFileSync(`${this.path}.journal.jsonl.tmp`, '');
      renameSync(`${this.path}.journal.jsonl.tmp`, `${this.path}.journal.jsonl`);
    }
    return accepted;
  }

  report() {
    const records = Object.values(this.state.portfolios);
    return {
      version: this.state.version,
      generatedAtMs: this.state.generatedAtMs,
      uniquePortfolioCount: records.length,
      countsBySurface: Object.fromEntries(['following', 'trending', 'fire_moves', 'most_recent']
        .map(surface => [surface, records.filter(row => row.surfaces.includes(surface as InvoFeedSurface)).length])),
    };
  }

  private save() {
    const tmp = `${this.path}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.state, null, 2));
    renameSync(tmp, this.path);
  }
}
