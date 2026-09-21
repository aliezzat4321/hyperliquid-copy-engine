import {
  appendFileSync, closeSync, existsSync, fsyncSync, mkdirSync, openSync,
  readFileSync, renameSync, statSync, writeFileSync,
} from 'fs';
import { createHash } from 'crypto';
import { dirname } from 'path';
import type { InvoFeedSurface } from './feed-surfaces.js';

export const FEED_PORTFOLIO_EVIDENCE_VERSION = 'lane3-feed-portfolio-evidence-v4-20260921';
export const FEED_EVIDENCE_EPOCH = 'lane3-feed-candidates-v4-20260921';
export const FEED_EVIDENCE_MAX_PORTFOLIOS = 1_000;
export const FEED_EVIDENCE_MAX_OBSERVATIONS_PER_PORTFOLIO = 2;
export const FEED_EVIDENCE_MAX_RECORD_BYTES = 4_096;
export const FEED_EVIDENCE_MAX_STATE_BYTES = 3_000_000;
export const FEED_EVIDENCE_MAX_JOURNAL_BYTES = 512_000;
export const FEED_EVIDENCE_SELECTOR_TTL_MS = 7 * 24 * 60 * 60_000;
export const FEED_EVIDENCE_COMPACT_INTERVAL_MS = 5 * 60_000;
export const FEED_REPLAY_BLOOM_BYTES = 256 * 1024;
const FEED_REPLAY_BLOOM_HASHES = 7;

export interface FeedPortfolioObservation {
  evidenceId: string; surface: InvoFeedSurface; postId: string | null;
  sourceTradeAtMs: number | null; capturedAtMs: number; firstObservedAtMs: number;
  processedAtMs: number; epoch: typeof FEED_EVIDENCE_EPOCH; portfolioId: string;
  ownerId: string | null; username: string | null; verified: boolean | null;
  portfolioCreatedAtMs: number | null; profile: Record<string, unknown>;
  rawPortfolioShapeKeys: string[]; rawPostShapeKeys: string[]; rawUpdateShapeKeys: string[];
}
export interface FeedPortfolioRecord {
  portfolioId: string; firstSeenAtMs: number; lastSeenAtMs: number; surfaces: InvoFeedSurface[];
  ownerId: string | null; username: string | null; observations: FeedPortfolioObservation[];
}
interface Telemetry {
  evidenceBytes: number; journalBytes: number; lastWriteDurationMs: number;
  rejectedIdentityConflicts: number; rejectedOversize: number;
  dedupedReplayCount: number; fullRewriteCount: number;
}
export interface FeedEvidenceState {
  version: typeof FEED_PORTFOLIO_EVIDENCE_VERSION; epoch: typeof FEED_EVIDENCE_EPOCH;
  generatedAtMs: number; portfolios: Record<string, FeedPortfolioRecord>; telemetry: Telemetry;
}

const obj = (v: unknown): Record<string, any> | null => v != null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, any> : null;
const txt = (v: unknown): string | null => typeof v === 'string' && v.trim() ? v.trim() : null;
const user = (v: unknown): string | null => txt(v)?.replace(/^@/, '').toLowerCase() ?? null;
function time(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v < 1e10 ? v * 1000 : v;
  if (typeof v !== 'string' || !v) return null;
  const n = Date.parse(v); return Number.isFinite(n) ? n : null;
}
const uniq = (values: Array<string | null>) => [...new Set(values.filter((v): v is string => v != null))];
const emptyTelemetry = (): Telemetry => ({
  evidenceBytes: 0, journalBytes: 0, lastWriteDurationMs: 0,
  rejectedIdentityConflicts: 0, rejectedOversize: 0, dedupedReplayCount: 0, fullRewriteCount: 0,
});
const emptyState = (): FeedEvidenceState => ({
  version: FEED_PORTFOLIO_EVIDENCE_VERSION, epoch: FEED_EVIDENCE_EPOCH,
  generatedAtMs: 0, portfolios: {}, telemetry: emptyTelemetry(),
});

class DurableReplayBloom {
  private bits: Buffer;
  private dirty = false;
  constructor(private readonly path: string) {
    mkdirSync(dirname(path), { recursive: true });
    if (existsSync(path)) {
      if (statSync(path).size !== FEED_REPLAY_BLOOM_BYTES) {
        throw new Error('feed replay bloom size mismatch');
      }
      this.bits = readFileSync(path);
    } else {
      this.bits = Buffer.alloc(FEED_REPLAY_BLOOM_BYTES);
    }
  }
  private indexes(id: string) {
    const digest = createHash('sha256').update(id).digest();
    const indexes: number[] = [];
    for (let i = 0; i < FEED_REPLAY_BLOOM_HASHES; i++) {
      indexes.push(digest.readUInt32BE((i * 4) % 28) % (FEED_REPLAY_BLOOM_BYTES * 8));
    }
    return indexes;
  }
  has(id: string) {
    return this.indexes(id).every(bit => (this.bits[bit >>> 3] & (1 << (bit & 7))) !== 0);
  }
  add(id: string) {
    for (const bit of this.indexes(id)) this.bits[bit >>> 3] |= 1 << (bit & 7);
    this.dirty = true;
  }
  flush() {
    if (!this.dirty) return;
    const tmp = `${this.path}.tmp`;
    const fd = openSync(tmp, 'w', 0o600);
    try { writeFileSync(fd, this.bits); fsyncSync(fd); } finally { closeSync(fd); }
    renameSync(tmp, this.path);
    try {
      const dfd = openSync(dirname(this.path), 'r');
      try { fsyncSync(dfd); } finally { closeSync(dfd); }
    } catch {}
    this.dirty = false;
  }
}

function provenance(post: any, update: Record<string, any>, portfolio: Record<string, any>) {
  const po = obj(post?.owner), uo = obj(update.owner), fo = obj(portfolio.owner) ?? obj(portfolio.user);
  const claims = [
    { ownerId: txt(post?.ownerId) ?? txt(po?.id), username: user(post?.username) ?? user(po?.username) },
    { ownerId: txt(update.ownerId) ?? txt(uo?.id), username: user(update.username) ?? user(uo?.username) },
    { ownerId: txt(portfolio.ownerId) ?? txt(fo?.id), username: user(portfolio.username) ?? user(fo?.username) },
  ].filter(claim => claim.ownerId || claim.username);
  const ids = uniq(claims.map(claim => claim.ownerId));
  const names = uniq(claims.map(claim => claim.username));
  if (ids.length > 1) return { reason: 'conflicting_owner_ids' };
  if (names.length > 1) return { reason: 'conflicting_usernames' };
  if ([po, uo, fo].filter(Boolean).length > 1 && !ids.length && !names.length) return { reason: 'ambiguous_owner_metadata' };
  const ownerId = ids[0] ?? null;
  const username = names[0] ?? null;
  if (ownerId && username && !claims.some(claim => claim.ownerId === ownerId && claim.username === username)) {
    return { reason: 'unattested_owner_username_pair' };
  }
  return { ownerId, username };
}

function boundOwnerVerification(
  update: Record<string, any>, portfolio: Record<string, any>,
  identity: { ownerId?: string | null; username?: string | null },
): boolean | null {
  if (!identity.ownerId || !identity.username) return null;
  const owners = [obj(update.owner), obj(portfolio.owner), obj(portfolio.user)].filter(Boolean) as Record<string, any>[];
  const values: boolean[] = [];
  for (const owner of owners) {
    if (typeof owner.verified !== 'boolean') continue;
    if (txt(owner.id) !== identity.ownerId || user(owner.username) !== identity.username) continue;
    values.push(owner.verified);
  }
  if (values.some(v => !v)) return false;
  return values.length > 0 && values.every(Boolean) ? true : null;
}

const PROFILE_FIELDS = [
  'id','portfolioId','_id','name','title','portfolioName','createdAt','created_at',
  'closedPositions','closedPositionsCount','closedTrades','totalClosedPositions',
  'openPositions','openPositionsCount','openTrades','wonPositions','wonPositionsCount',
  'winningPositions','wins','lostPositions','lostPositionsCount','losingPositions','losses',
  'winRate','win_rate','winRatePct','percentChange','pnlPercent','profitLossPercent','roi',
  'lastTradeAt','last_trade_at','lastPositionAt','last_position_at','lastClosedPositionAt',
  'last_closed_position_at','lastActivityAt','last_activity_at','liquidated','isLiquidated',
  'currentWinStreak','winStreak','followerCount','followers',
] as const;

export type FeedNormalizationResult = {
  observation: FeedPortfolioObservation | null; rejectionReason: string | null;
};
export function normalizeFeedPortfolioObservationDetailed(
  post: any, surface: InvoFeedSurface, capturedAtMs: number,
): FeedNormalizationResult {
  const update = obj(post?.update) ?? obj(post?.investment) ?? obj(post?.data) ?? {};
  const portfolio = obj(update.portfolio) ?? obj(post?.portfolio);
  if (!portfolio) return { observation: null, rejectionReason: 'missing_portfolio' };
  const portfolioId = txt(portfolio.id ?? portfolio.portfolioId ?? portfolio._id);
  if (!portfolioId) return { observation: null, rejectionReason: 'missing_portfolio_id' };
  const p = provenance(post, update, portfolio);
  if ('reason' in p) return { observation: null, rejectionReason: p.reason! };
  const profile = Object.fromEntries(PROFILE_FIELDS.filter(k => portfolio[k] !== undefined).map(k => [k, portfolio[k]]));
  const sourceTradeAtMs = time(update.closedAt ?? update.updatedAt ?? update.createdAt ?? post?.updatedAt ?? post?.createdAt);
  const postId = txt(post?.id ?? post?._id);
  const verified = boundOwnerVerification(update, portfolio, p);
  const observation: FeedPortfolioObservation = {
    evidenceId: createHash('sha256').update(JSON.stringify([
      surface, postId, portfolioId, sourceTradeAtMs, p.ownerId, p.username, profile,
    ])).digest('hex'),
    surface, postId, sourceTradeAtMs, capturedAtMs, firstObservedAtMs: capturedAtMs,
    processedAtMs: capturedAtMs, epoch: FEED_EVIDENCE_EPOCH, portfolioId,
    ownerId: p.ownerId!, username: p.username!, verified,
    portfolioCreatedAtMs: time(portfolio.createdAt ?? portfolio.created_at), profile,
    rawPortfolioShapeKeys: Object.keys(portfolio).sort().slice(0, 128),
    rawPostShapeKeys: Object.keys(obj(post) ?? {}).sort().slice(0, 128),
    rawUpdateShapeKeys: Object.keys(update).sort().slice(0, 128),
  };
  if (Buffer.byteLength(JSON.stringify(observation)) > FEED_EVIDENCE_MAX_RECORD_BYTES) {
    return { observation: null, rejectionReason: 'record_byte_cap_exceeded' };
  }
  return { observation, rejectionReason: null };
}
export function normalizeFeedPortfolioObservation(post: any, surface: InvoFeedSurface, capturedAtMs: number) {
  return normalizeFeedPortfolioObservationDetailed(post, surface, capturedAtMs).observation;
}

function identityConflict(prior: FeedPortfolioRecord | undefined, row: FeedPortfolioObservation) {
  if (!prior) return false;
  return Boolean(
    (prior.ownerId && row.ownerId && prior.ownerId !== row.ownerId)
    || (prior.username && row.username && prior.username !== row.username)
    || (prior.ownerId && !prior.username && !row.ownerId && row.username)
    || (prior.username && !prior.ownerId && row.ownerId && !row.username)
  );
}

function apply(state: FeedEvidenceState, row: FeedPortfolioObservation, replayIds: Set<string>) {
  if (row.epoch !== FEED_EVIDENCE_EPOCH || replayIds.has(row.evidenceId)) return false;
  const prior = state.portfolios[row.portfolioId];
  if (identityConflict(prior, row)) {
    state.telemetry.rejectedIdentityConflicts += 1;
    return false;
  }
  replayIds.add(row.evidenceId);
  // Preserve partial claims so a later opposite-half claim can be rejected. A later
  // full row may safely complete the pair only because identityConflict requires its
  // overlapping claim to agree with the retained provenance.
  const ownerId = prior?.ownerId ?? row.ownerId;
  const username = prior?.username ?? row.username;
  state.portfolios[row.portfolioId] = {
    portfolioId: row.portfolioId,
    firstSeenAtMs: Math.min(prior?.firstSeenAtMs ?? row.capturedAtMs, row.capturedAtMs),
    lastSeenAtMs: Math.max(prior?.lastSeenAtMs ?? 0, row.capturedAtMs),
    surfaces: [...new Set([...(prior?.surfaces ?? []), row.surface])].sort() as InvoFeedSurface[],
    ownerId, username,
    observations: [...(prior?.observations ?? []), row]
      .sort((a,b) => a.capturedAtMs-b.capturedAtMs || a.evidenceId.localeCompare(b.evidenceId))
      .slice(-FEED_EVIDENCE_MAX_OBSERVATIONS_PER_PORTFOLIO),
  };
  state.generatedAtMs = Math.max(state.generatedAtMs, row.capturedAtMs);
  return true;
}

const byteSize = (state: FeedEvidenceState) => Buffer.byteLength(JSON.stringify(state));
function bound(state: FeedEvidenceState) {
  const kept = Object.values(state.portfolios)
    .sort((a,b) => b.lastSeenAtMs-a.lastSeenAtMs || a.portfolioId.localeCompare(b.portfolioId))
    .slice(0, FEED_EVIDENCE_MAX_PORTFOLIOS);
  state.portfolios = Object.fromEntries(kept.map(r => [r.portfolioId, r]));
  while (byteSize(state) > FEED_EVIDENCE_MAX_STATE_BYTES && Object.keys(state.portfolios).length) {
    const oldest = Object.values(state.portfolios).sort((a,b) => a.lastSeenAtMs-b.lastSeenAtMs)[0];
    delete state.portfolios[oldest.portfolioId];
  }
  state.telemetry.evidenceBytes = byteSize(state);
}

function journalEvidenceIds(path: string): string[] {
  const journal = `${path}.journal.jsonl`;
  if (!existsSync(journal)) return [];
  const ids: string[] = [];
  for (const line of readFileSync(journal, 'utf8').split('\n')) {
    if (!line.trim()) continue;
    try {
      const parsed = JSON.parse(line);
      if (typeof parsed?.evidenceId === 'string' && parsed.evidenceId) ids.push(parsed.evidenceId);
    } catch {}
  }
  return ids;
}

export function loadFeedPortfolioEvidence(path: string): FeedEvidenceState {
  let state = emptyState();
  let compatibleSnapshot = !existsSync(path);
  if (existsSync(path)) {
    try {
      const p = JSON.parse(readFileSync(path,'utf8'));
      if (p.version === FEED_PORTFOLIO_EVIDENCE_VERSION && p.epoch === FEED_EVIDENCE_EPOCH && p.portfolios) {
        state = { ...p, telemetry: { ...emptyTelemetry(), ...p.telemetry } };
        compatibleSnapshot = true;
      }
    } catch {}
  }
  const replayIds = new Set(Object.values(state.portfolios)
    .flatMap(record => record.observations.map(row => row.evidenceId)));
  const journal = `${path}.journal.jsonl`;
  if (compatibleSnapshot && existsSync(journal)) {
    for (const line of readFileSync(journal,'utf8').split('\n')) {
      if (!line.trim()) continue;
      try { apply(state, JSON.parse(line), replayIds); } catch {}
    }
  }
  bound(state);
  state.telemetry.journalBytes = existsSync(journal) ? statSync(journal).size : 0;
  return state;
}

export class FeedPortfolioEvidenceStore {
  private state: FeedEvidenceState;
  private replayIds: Set<string>;
  private lastCompactedAtMs: number;
  private replayBloom: DurableReplayBloom;

  constructor(private readonly path: string) {
    mkdirSync(dirname(path), { recursive: true });
    this.state = loadFeedPortfolioEvidence(path);
    this.replayIds = new Set([
      ...Object.values(this.state.portfolios).flatMap(record => record.observations.map(row => row.evidenceId)),
      ...journalEvidenceIds(path),
    ]);
    this.replayBloom = new DurableReplayBloom(`${path}.replay.bloom`);
    for (const id of this.replayIds) this.replayBloom.add(id);
    this.replayBloom.flush();
    this.lastCompactedAtMs = existsSync(path) ? statSync(path).mtimeMs : 0;
  }

  observe(posts: any[], surface: InvoFeedSurface, capturedAtMs = Date.now()) {
    const started = performance.now();
    mkdirSync(dirname(this.path), { recursive: true });
    const journal = `${this.path}.journal.jsonl`;
    const accepted: FeedPortfolioObservation[] = [];
    const candidates: FeedPortfolioObservation[] = [];
    const pendingIds = new Set<string>();

    for (const post of posts) {
      const n = normalizeFeedPortfolioObservationDetailed(post, surface, capturedAtMs);
      if (!n.observation) {
        if (n.rejectionReason?.includes('owner') || n.rejectionReason?.includes('username')) {
          this.state.telemetry.rejectedIdentityConflicts++;
        }
        if (n.rejectionReason === 'record_byte_cap_exceeded') this.state.telemetry.rejectedOversize++;
        continue;
      }
      const row = n.observation;
      if (this.replayIds.has(row.evidenceId) || this.replayBloom.has(row.evidenceId) || pendingIds.has(row.evidenceId)) {
        this.state.telemetry.dedupedReplayCount++;
        continue;
      }
      pendingIds.add(row.evidenceId);
      candidates.push(row);
    }

    // Replay memory is committed once for the whole batch before any observation
    // journal write. If later persistence fails, discovery loses the row conservatively
    // rather than allowing the same historical evidence to be re-stamped as fresh.
    for (const row of candidates) this.replayBloom.add(row.evidenceId);
    this.replayBloom.flush();

    for (const row of candidates) {
      const line = `${JSON.stringify(row)}
`;
      if ((existsSync(journal) ? statSync(journal).size : 0) + Buffer.byteLength(line) > FEED_EVIDENCE_MAX_JOURNAL_BYTES) {
        this.compact(capturedAtMs);
      }
      appendFileSync(journal, line);
      if (!apply(this.state, row, this.replayIds)) {
        // Rejected identity evidence remains in replay memory so it cannot later be
        // reintroduced with a fresh processing timestamp.
        continue;
      }
      accepted.push(row);
    }

    bound(this.state);
    this.state.telemetry.journalBytes = existsSync(journal) ? statSync(journal).size : 0;
    if (!existsSync(this.path) || capturedAtMs - this.lastCompactedAtMs >= FEED_EVIDENCE_COMPACT_INTERVAL_MS) {
      this.compact(capturedAtMs);
    }
    this.state.telemetry.lastWriteDurationMs = Math.round((performance.now() - started) * 1000) / 1000;
    return accepted;
  }

  report() {
    const records = Object.values(this.state.portfolios);
    return {
      version: this.state.version, epoch: this.state.epoch, generatedAtMs: this.state.generatedAtMs,
      uniquePortfolioCount: records.length,
      limits: {
        maxPortfolios: FEED_EVIDENCE_MAX_PORTFOLIOS,
        maxObservationsPerPortfolio: FEED_EVIDENCE_MAX_OBSERVATIONS_PER_PORTFOLIO,
        maxRecordBytes: FEED_EVIDENCE_MAX_RECORD_BYTES, maxStateBytes: FEED_EVIDENCE_MAX_STATE_BYTES,
        maxJournalBytes: FEED_EVIDENCE_MAX_JOURNAL_BYTES, replayBloomBytes: FEED_REPLAY_BLOOM_BYTES,
        selectorTtlMs: FEED_EVIDENCE_SELECTOR_TTL_MS, compactIntervalMs: FEED_EVIDENCE_COMPACT_INTERVAL_MS,
      },
      ...this.state.telemetry,
      countsBySurface: Object.fromEntries(['following','trending','fire_moves','most_recent'].map(
        s => [s, records.filter(r => r.surfaces.includes(s as InvoFeedSurface)).length],
      )),
    };
  }

  private compact(nowMs: number) {
    // Never discard journal recovery evidence until all accepted IDs are in the durable Bloom.
    this.replayBloom.flush();
    bound(this.state);
    this.state.telemetry.fullRewriteCount++;
    const tmp = `${this.path}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.state));
    if (statSync(tmp).size > FEED_EVIDENCE_MAX_STATE_BYTES) throw new Error('feed evidence state byte cap exceeded');
    renameSync(tmp, this.path);
    const jt = `${this.path}.journal.jsonl.tmp`;
    writeFileSync(jt, '');
    renameSync(jt, `${this.path}.journal.jsonl`);
    this.state.telemetry.journalBytes = 0;
    this.replayIds = new Set(Object.values(this.state.portfolios)
      .flatMap(record => record.observations.map(row => row.evidenceId)));
    this.lastCompactedAtMs = nowMs;
  }
}
