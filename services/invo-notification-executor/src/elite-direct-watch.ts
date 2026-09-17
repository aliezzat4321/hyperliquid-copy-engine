import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs';
import { dirname } from 'path';
import type { InvoSignal } from './notification-signal.js';

export const ELITE_DIRECT_WATCH_VERSION = 'lane3-elite-direct-watch-v1-20260917';

export interface EliteDirectTarget {
  portfolioId: string;
  ownerId: string;
  username: string;
  sourceFilter: string;
}

interface StoredTarget extends EliteDirectTarget {
  baselineAtMs: number;
  processedThroughMs: number;
  selectorInitialized: boolean;
  lastSelectorUpdatedAtMs: number | null;
  lastFallbackPollAtMs: number;
}

interface DirectWatchDiskState {
  version: string;
  targets: Record<string, StoredTarget>;
}

export interface DirectWatchStatus {
  version: string;
  targetCount: number;
  selectorInitializedCount: number;
  fallbackTargetCount: number;
  oldestProcessedThroughMs: number | null;
}

export function directSourceTimeMs(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value < 10_000_000_000 ? value * 1000 : value;
  if (typeof value !== 'string' || !value) return null;
  const asNumber = Number(value);
  if (Number.isFinite(asNumber) && asNumber > 0) return asNumber < 10_000_000_000 ? asNumber * 1000 : asNumber;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function positive(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function normalizedUsername(value: unknown): string {
  return String(value ?? '').replace(/^@/, '').trim().toLowerCase();
}

export function loadEliteDirectTargets(
  candidateStatePath: string,
  nowMs: number,
  maxAgeMs: number,
): { targets: EliteDirectTarget[]; observedAtMs: number | null; stale: boolean } {
  if (!existsSync(candidateStatePath)) return { targets: [], observedAtMs: null, stale: true };
  const parsed = JSON.parse(readFileSync(candidateStatePath, 'utf8')) as any;
  const observedAtMs = Number(parsed?.lastObservedAtMs);
  const stale = !Number.isFinite(observedAtMs) || nowMs - observedAtMs > maxAgeMs;
  if (stale) return { targets: [], observedAtMs: Number.isFinite(observedAtMs) ? observedAtMs : null, stale: true };
  const targets: EliteDirectTarget[] = [];
  for (const row of Object.values(parsed?.portfolios ?? {}) as any[]) {
    if (row?.bucket !== 'ELITE_CANDIDATE') continue;
    const portfolioId = String(row?.portfolioId ?? '').trim();
    const ownerId = String(row?.ownerId ?? '').trim();
    const username = normalizedUsername(row?.username);
    const sourceFilter = String(row?.sourceFilter ?? '').trim().toLowerCase();
    if (!portfolioId || !ownerId || !username || !sourceFilter) continue;
    targets.push({ portfolioId, ownerId, username, sourceFilter });
  }
  targets.sort((a, b) => a.portfolioId.localeCompare(b.portfolioId));
  return { targets, observedAtMs, stale: false };
}

export class EliteDirectWatchState {
  private state: DirectWatchDiskState = { version: ELITE_DIRECT_WATCH_VERSION, targets: {} };

  constructor(private readonly path: string) {
    this.load();
  }

  private load() {
    if (!existsSync(this.path)) return;
    try {
      const parsed = JSON.parse(readFileSync(this.path, 'utf8')) as DirectWatchDiskState;
      if (parsed?.version === ELITE_DIRECT_WATCH_VERSION && parsed?.targets && typeof parsed.targets === 'object') {
        this.state = parsed;
      }
    } catch {
      // Fail closed by starting a new prospective baseline. Historical events are never replayed.
    }
  }

  private save() {
    mkdirSync(dirname(this.path), { recursive: true });
    const temp = `${this.path}.tmp`;
    writeFileSync(temp, JSON.stringify(this.state, null, 2));
    renameSync(temp, this.path);
  }

  syncTargets(targets: EliteDirectTarget[], ownedPortfolioIds: Set<string>, baselineAtMs: number) {
    const wanted = new Map(targets.map(target => [target.portfolioId, target]));
    let changed = false;
    for (const target of targets) {
      const existing = this.state.targets[target.portfolioId];
      if (!existing) {
        this.state.targets[target.portfolioId] = {
          ...target,
          baselineAtMs,
          processedThroughMs: baselineAtMs,
          selectorInitialized: false,
          lastSelectorUpdatedAtMs: null,
          lastFallbackPollAtMs: 0,
        };
        changed = true;
      } else if (
        existing.ownerId !== target.ownerId
        || existing.username !== target.username
        || existing.sourceFilter !== target.sourceFilter
      ) {
        this.state.targets[target.portfolioId] = { ...existing, ...target };
        changed = true;
      }
    }
    for (const portfolioId of Object.keys(this.state.targets)) {
      if (!wanted.has(portfolioId) && !ownedPortfolioIds.has(portfolioId)) {
        delete this.state.targets[portfolioId];
        changed = true;
      }
    }
    if (changed) this.save();
  }

  targets(): StoredTarget[] {
    return Object.values(this.state.targets).map(target => ({ ...target }));
  }

  observeSelector(portfolioId: string, selectorUpdatedAtMs: number): { hydrate: boolean; processedThroughMs: number } {
    const target = this.state.targets[portfolioId];
    if (!target) return { hydrate: false, processedThroughMs: 0 };
    if (!target.selectorInitialized) {
      target.selectorInitialized = true;
      target.lastSelectorUpdatedAtMs = selectorUpdatedAtMs;
      this.save();
      return { hydrate: false, processedThroughMs: target.processedThroughMs };
    }
    return {
      hydrate: target.lastSelectorUpdatedAtMs == null || selectorUpdatedAtMs > target.lastSelectorUpdatedAtMs,
      processedThroughMs: target.processedThroughMs,
    };
  }

  shouldFallbackPoll(portfolioId: string, nowMs: number, intervalMs: number): boolean {
    const target = this.state.targets[portfolioId];
    return Boolean(target && nowMs - target.lastFallbackPollAtMs >= intervalMs);
  }

  noteFallbackPoll(portfolioId: string, atMs: number) {
    const target = this.state.targets[portfolioId];
    if (!target) return;
    target.lastFallbackPollAtMs = atMs;
    this.save();
  }

  commitHydration(portfolioId: string, processedThroughMs: number, selectorUpdatedAtMs?: number) {
    const target = this.state.targets[portfolioId];
    if (!target) return;
    target.processedThroughMs = Math.max(target.processedThroughMs, processedThroughMs);
    if (selectorUpdatedAtMs != null) {
      target.selectorInitialized = true;
      target.lastSelectorUpdatedAtMs = Math.max(target.lastSelectorUpdatedAtMs ?? 0, selectorUpdatedAtMs);
    }
    this.save();
  }

  status(): DirectWatchStatus {
    const targets = Object.values(this.state.targets);
    const processed = targets.map(target => target.processedThroughMs).filter(Number.isFinite);
    return {
      version: ELITE_DIRECT_WATCH_VERSION,
      targetCount: targets.length,
      selectorInitializedCount: targets.filter(target => target.selectorInitialized).length,
      fallbackTargetCount: targets.filter(target => !target.selectorInitialized).length,
      oldestProcessedThroughMs: processed.length ? Math.min(...processed) : null,
    };
  }
}

function signalFromInvestment(
  row: any,
  target: EliteDirectTarget,
  action: InvoSignal['action'],
  sourceTimeMs: number,
  sourceTimeField: string,
  observedAtMs: number,
  entrySizeOverride?: number,
  eventIdentityOverride?: string,
): InvoSignal | null {
  if (row?.verifiedTrade !== true) return null;
  const rowPortfolioId = String(row?.portfolio?.id ?? target.portfolioId);
  if (rowPortfolioId && rowPortfolioId !== target.portfolioId) return null;
  const sourceBaseId = String(row?.baseId ?? row?.id ?? '').trim();
  const investmentId = String(row?.id ?? sourceBaseId).trim();
  const coin = String(row?.ticker ?? '').trim().toUpperCase();
  const leverage = positive(row?.leverage);
  if (!sourceBaseId || !investmentId || !coin || leverage == null || typeof row?.directionLong !== 'boolean') return null;
  const eventIdentity = eventIdentityOverride ?? String(sourceTimeMs);
  const postId = `direct-investment:${target.portfolioId}:${investmentId}:${action}:${eventIdentity}`;
  return {
    key: `${postId}:${action}:${sourceBaseId}`,
    postId,
    action,
    observedAtMs,
    sourceTimeMs,
    sourceTimeField,
    ownerId: target.ownerId || String(row?.owner?.id ?? ''),
    username: target.username || normalizedUsername(row?.owner?.username),
    portfolioId: target.portfolioId,
    sourceBaseId,
    sourceBaseShortId: String(row?.baseShortId ?? ''),
    coin,
    side: row.directionLong ? 'long' : 'short',
    leverage: Math.max(1, Math.trunc(leverage)),
    entryPrice: positive(row?.entryPrice),
    closingPrice: positive(row?.closingPrice),
    entrySize: entrySizeOverride ?? positive(row?.entrySize),
  };
}

export function signalsFromDirectInvestments(
  openRows: any[],
  closedRows: any[],
  target: EliteDirectTarget,
  processedThroughMs: number,
  observedAtMs: number,
): InvoSignal[] {
  const out: InvoSignal[] = [];
  for (const row of openRows) {
    if (row?.isOpen !== true || row?.verifiedTrade !== true) continue;
    const createdAtMs = directSourceTimeMs(row?.createdAt);
    const updatedAtMs = directSourceTimeMs(row?.updatedAt);
    if (createdAtMs != null && createdAtMs > processedThroughMs) {
      const signal = signalFromInvestment(row, target, 'open', createdAtMs, 'investment.createdAt', observedAtMs);
      if (signal) out.push(signal);
      continue;
    }
    if (updatedAtMs == null || updatedAtMs <= processedThroughMs || row?.changes?.simIncrease !== true) continue;
    const currentSize = positive(row?.entrySize);
    const priorSize = positive(row?.changes?.entrySize);
    if (currentSize == null || priorSize == null || currentSize <= priorSize) continue;
    const signal = signalFromInvestment(
      row,
      target,
      'increase',
      updatedAtMs,
      'investment.updatedAt',
      observedAtMs,
      currentSize - priorSize,
      `size-${currentSize}`,
    );
    if (signal) out.push(signal);
  }
  for (const row of closedRows) {
    if (row?.isOpen !== false || row?.verifiedTrade !== true || positive(row?.closingPrice) == null) continue;
    const closedAtMs = directSourceTimeMs(row?.closedAt) ?? directSourceTimeMs(row?.updatedAt);
    if (closedAtMs == null || closedAtMs <= processedThroughMs) continue;
    const signal = signalFromInvestment(
      row,
      target,
      'close',
      closedAtMs,
      row?.closedAt ? 'investment.closedAt' : 'investment.updatedAt',
      observedAtMs,
    );
    if (signal) out.push(signal);
  }
  const byKey = new Map(out.map(signal => [signal.key, signal]));
  return [...byKey.values()].sort((a, b) => (a.sourceTimeMs ?? a.observedAtMs) - (b.sourceTimeMs ?? b.observedAtMs));
}
