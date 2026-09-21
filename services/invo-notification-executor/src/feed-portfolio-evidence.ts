import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from 'fs';
import { createHash } from 'crypto';
import { dirname } from 'path';
import type { InvoFeedSurface } from './feed-surfaces.js';

export const FEED_PORTFOLIO_EVIDENCE_VERSION = 'lane3-feed-portfolio-evidence-v3-20260921';
export const FEED_EVIDENCE_EPOCH = 'lane3-feed-candidates-v3-20260921';
export const FEED_EVIDENCE_MAX_PORTFOLIOS = 1_000;
export const FEED_EVIDENCE_MAX_OBSERVATIONS_PER_PORTFOLIO = 2;
export const FEED_EVIDENCE_MAX_RECORD_BYTES = 4_096;
export const FEED_EVIDENCE_MAX_STATE_BYTES = 3_000_000;
export const FEED_EVIDENCE_MAX_JOURNAL_BYTES = 512_000;
export const FEED_EVIDENCE_SELECTOR_TTL_MS = 7 * 24 * 60 * 60_000;
export const FEED_EVIDENCE_COMPACT_INTERVAL_MS = 5 * 60_000;

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
interface Telemetry { evidenceBytes: number; journalBytes: number; lastWriteDurationMs: number; rejectedIdentityConflicts: number; rejectedOversize: number; dedupedReplayCount: number; fullRewriteCount: number }
export interface FeedEvidenceState { version: typeof FEED_PORTFOLIO_EVIDENCE_VERSION; epoch: typeof FEED_EVIDENCE_EPOCH; generatedAtMs: number; portfolios: Record<string, FeedPortfolioRecord>; telemetry: Telemetry }

const obj = (v: unknown): Record<string, any> | null => v != null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, any> : null;
const txt = (v: unknown): string | null => typeof v === 'string' && v.trim() ? v.trim() : null;
const user = (v: unknown): string | null => txt(v)?.replace(/^@/, '').toLowerCase() ?? null;
function time(v: unknown): number | null { if (typeof v === 'number' && Number.isFinite(v)) return v < 1e10 ? v * 1000 : v; if (typeof v !== 'string' || !v) return null; const n = Date.parse(v); return Number.isFinite(n) ? n : null; }
const uniq = (values: Array<string | null>) => [...new Set(values.filter((v): v is string => v != null))];
const emptyTelemetry = (): Telemetry => ({ evidenceBytes: 0, journalBytes: 0, lastWriteDurationMs: 0, rejectedIdentityConflicts: 0, rejectedOversize: 0, dedupedReplayCount: 0, fullRewriteCount: 0 });
const emptyState = (): FeedEvidenceState => ({ version: FEED_PORTFOLIO_EVIDENCE_VERSION, epoch: FEED_EVIDENCE_EPOCH, generatedAtMs: 0, portfolios: {}, telemetry: emptyTelemetry() });

function provenance(post: any, update: Record<string, any>, portfolio: Record<string, any>) {
  const po = obj(post?.owner), uo = obj(update.owner), fo = obj(portfolio.owner) ?? obj(portfolio.user);
  const ids = uniq([txt(post?.ownerId), txt(po?.id), txt(update.ownerId), txt(uo?.id), txt(portfolio.ownerId), txt(fo?.id)]);
  const names = uniq([user(post?.username), user(po?.username), user(update.username), user(uo?.username), user(portfolio.username), user(fo?.username)]);
  if (ids.length > 1) return { reason: 'conflicting_owner_ids' };
  if (names.length > 1) return { reason: 'conflicting_usernames' };
  if ([po, uo, fo].filter(Boolean).length > 1 && !ids.length && !names.length) return { reason: 'ambiguous_owner_metadata' };
  return { ownerId: ids[0] ?? null, username: names[0] ?? null };
}
const PROFILE_FIELDS = ['id','portfolioId','_id','name','title','portfolioName','createdAt','created_at','closedPositions','closedPositionsCount','closedTrades','totalClosedPositions','openPositions','openPositionsCount','openTrades','wonPositions','wonPositionsCount','winningPositions','wins','lostPositions','lostPositionsCount','losingPositions','losses','winRate','win_rate','winRatePct','percentChange','pnlPercent','profitLossPercent','roi','lastTradeAt','last_trade_at','lastPositionAt','last_position_at','lastClosedPositionAt','last_closed_position_at','lastActivityAt','last_activity_at','liquidated','isLiquidated','currentWinStreak','winStreak','followerCount','followers'] as const;
function verification(post: any, update: Record<string, any>, portfolio: Record<string, any>): boolean | null {
  const values = [obj(post?.owner)?.verified,post?.verified,post?.isVerified,obj(update.owner)?.verified,update.verified,update.isVerified,obj(portfolio.owner)?.verified,obj(portfolio.user)?.verified,portfolio.verified,portfolio.isVerified].filter(v => typeof v === 'boolean') as boolean[];
  return values.some(v => !v) ? false : values.length && values.every(Boolean) ? true : null;
}
export type FeedNormalizationResult = { observation: FeedPortfolioObservation | null; rejectionReason: string | null };
export function normalizeFeedPortfolioObservationDetailed(post: any, surface: InvoFeedSurface, capturedAtMs: number): FeedNormalizationResult {
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
  const observation: FeedPortfolioObservation = {
    evidenceId: createHash('sha256').update(JSON.stringify([surface, postId, portfolioId, sourceTradeAtMs, profile])).digest('hex'),
    surface, postId, sourceTradeAtMs, capturedAtMs, firstObservedAtMs: capturedAtMs,
    processedAtMs: capturedAtMs, epoch: FEED_EVIDENCE_EPOCH, portfolioId, ownerId: p.ownerId!, username: p.username!,
    verified: verification(post, update, portfolio), portfolioCreatedAtMs: time(portfolio.createdAt ?? portfolio.created_at), profile,
    rawPortfolioShapeKeys: Object.keys(portfolio).sort().slice(0, 128), rawPostShapeKeys: Object.keys(obj(post) ?? {}).sort().slice(0, 128), rawUpdateShapeKeys: Object.keys(update).sort().slice(0, 128),
  };
  if (Buffer.byteLength(JSON.stringify(observation)) > FEED_EVIDENCE_MAX_RECORD_BYTES) return { observation: null, rejectionReason: 'record_byte_cap_exceeded' };
  return { observation, rejectionReason: null };
}
export function normalizeFeedPortfolioObservation(post: any, surface: InvoFeedSurface, capturedAtMs: number) { return normalizeFeedPortfolioObservationDetailed(post, surface, capturedAtMs).observation; }

function apply(state: FeedEvidenceState, row: FeedPortfolioObservation, replayIds: Set<string>) {
  if (row.epoch !== FEED_EVIDENCE_EPOCH || replayIds.has(row.evidenceId)) return false;
  const prior = state.portfolios[row.portfolioId];
  if (prior && ((prior.ownerId && row.ownerId && prior.ownerId !== row.ownerId) || (prior.username && row.username && prior.username !== row.username))) { state.telemetry.rejectedIdentityConflicts++; return false; }
  replayIds.add(row.evidenceId);
  state.portfolios[row.portfolioId] = { portfolioId: row.portfolioId, firstSeenAtMs: Math.min(prior?.firstSeenAtMs ?? row.capturedAtMs, row.capturedAtMs), lastSeenAtMs: Math.max(prior?.lastSeenAtMs ?? 0, row.capturedAtMs), surfaces: [...new Set([...(prior?.surfaces ?? []), row.surface])].sort() as InvoFeedSurface[], ownerId: prior?.ownerId ?? row.ownerId, username: prior?.username ?? row.username, observations: [...(prior?.observations ?? []), row].sort((a,b) => a.capturedAtMs-b.capturedAtMs || a.evidenceId.localeCompare(b.evidenceId)).slice(-FEED_EVIDENCE_MAX_OBSERVATIONS_PER_PORTFOLIO) };
  state.generatedAtMs = Math.max(state.generatedAtMs, row.capturedAtMs); return true;
}
const byteSize = (state: FeedEvidenceState) => Buffer.byteLength(JSON.stringify(state));
function bound(state: FeedEvidenceState) {
  const kept = Object.values(state.portfolios).sort((a,b) => b.lastSeenAtMs-a.lastSeenAtMs || a.portfolioId.localeCompare(b.portfolioId)).slice(0, FEED_EVIDENCE_MAX_PORTFOLIOS);
  state.portfolios = Object.fromEntries(kept.map(r => [r.portfolioId, r]));
  while (byteSize(state) > FEED_EVIDENCE_MAX_STATE_BYTES && Object.keys(state.portfolios).length) { const oldest = Object.values(state.portfolios).sort((a,b) => a.lastSeenAtMs-b.lastSeenAtMs)[0]; delete state.portfolios[oldest.portfolioId]; }
  state.telemetry.evidenceBytes = byteSize(state);
}
export function loadFeedPortfolioEvidence(path: string): FeedEvidenceState {
  let state = emptyState();
  let compatibleSnapshot = !existsSync(path);
  if (existsSync(path)) try { const p = JSON.parse(readFileSync(path,'utf8')); if (p.version === FEED_PORTFOLIO_EVIDENCE_VERSION && p.epoch === FEED_EVIDENCE_EPOCH && p.portfolios) { state = { ...p, telemetry: { ...emptyTelemetry(), ...p.telemetry } }; compatibleSnapshot = true; } } catch {}
  const replayIds = new Set(Object.values(state.portfolios).flatMap(record => record.observations.map(row => row.evidenceId)));
  const journal = `${path}.journal.jsonl`;
  if (compatibleSnapshot && existsSync(journal)) for (const line of readFileSync(journal,'utf8').split('\n')) if (line.trim()) try { apply(state, JSON.parse(line), replayIds); } catch {}
  bound(state); state.telemetry.journalBytes = existsSync(journal) ? statSync(journal).size : 0; return state;
}
export class FeedPortfolioEvidenceStore {
  private state: FeedEvidenceState; private replayIds: Set<string>; private lastCompactedAtMs: number;
  constructor(private readonly path: string) { this.state = loadFeedPortfolioEvidence(path); this.replayIds = new Set(Object.values(this.state.portfolios).flatMap(record => record.observations.map(row => row.evidenceId))); this.lastCompactedAtMs = existsSync(path) ? statSync(path).mtimeMs : 0; }
  observe(posts: any[], surface: InvoFeedSurface, capturedAtMs = Date.now()) {
    const started = performance.now(); mkdirSync(dirname(this.path), { recursive: true }); const journal = `${this.path}.journal.jsonl`; const accepted: FeedPortfolioObservation[] = [];
    for (const post of posts) { const n = normalizeFeedPortfolioObservationDetailed(post,surface,capturedAtMs); if (!n.observation) { if (n.rejectionReason?.includes('owner') || n.rejectionReason?.includes('username')) this.state.telemetry.rejectedIdentityConflicts++; if (n.rejectionReason === 'record_byte_cap_exceeded') this.state.telemetry.rejectedOversize++; continue; } if (this.replayIds.has(n.observation.evidenceId)) { this.state.telemetry.dedupedReplayCount++; continue; } const line=`${JSON.stringify(n.observation)}\n`; if ((existsSync(journal)?statSync(journal).size:0)+Buffer.byteLength(line)>FEED_EVIDENCE_MAX_JOURNAL_BYTES) this.compact(capturedAtMs); if (apply(this.state,n.observation,this.replayIds)) { appendFileSync(journal,line); accepted.push(n.observation); } }
    bound(this.state); this.state.telemetry.journalBytes = existsSync(journal) ? statSync(journal).size : 0;
    if (!existsSync(this.path) || capturedAtMs-this.lastCompactedAtMs >= FEED_EVIDENCE_COMPACT_INTERVAL_MS) this.compact(capturedAtMs);
    this.state.telemetry.lastWriteDurationMs = Math.round((performance.now()-started)*1000)/1000; return accepted;
  }
  report() { const records = Object.values(this.state.portfolios); return { version:this.state.version,epoch:this.state.epoch,generatedAtMs:this.state.generatedAtMs,uniquePortfolioCount:records.length,limits:{maxPortfolios:FEED_EVIDENCE_MAX_PORTFOLIOS,maxObservationsPerPortfolio:FEED_EVIDENCE_MAX_OBSERVATIONS_PER_PORTFOLIO,maxRecordBytes:FEED_EVIDENCE_MAX_RECORD_BYTES,maxStateBytes:FEED_EVIDENCE_MAX_STATE_BYTES,maxJournalBytes:FEED_EVIDENCE_MAX_JOURNAL_BYTES,selectorTtlMs:FEED_EVIDENCE_SELECTOR_TTL_MS,compactIntervalMs:FEED_EVIDENCE_COMPACT_INTERVAL_MS},...this.state.telemetry,countsBySurface:Object.fromEntries(['following','trending','fire_moves','most_recent'].map(s => [s,records.filter(r => r.surfaces.includes(s as InvoFeedSurface)).length]))}; }
  private compact(nowMs:number) { bound(this.state); this.state.telemetry.fullRewriteCount++; const tmp=`${this.path}.tmp`; writeFileSync(tmp,JSON.stringify(this.state)); if(statSync(tmp).size>FEED_EVIDENCE_MAX_STATE_BYTES) throw new Error('feed evidence state byte cap exceeded'); renameSync(tmp,this.path); const jt=`${this.path}.journal.jsonl.tmp`; writeFileSync(jt,''); renameSync(jt,`${this.path}.journal.jsonl`); this.state.telemetry.journalBytes=0; this.replayIds=new Set(Object.values(this.state.portfolios).flatMap(record=>record.observations.map(row=>row.evidenceId))); this.lastCompactedAtMs=nowMs; }
}
