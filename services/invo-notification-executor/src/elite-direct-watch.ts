import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs';
import { dirname } from 'path';
import type { InvoSignal } from './notification-signal.js';

export const ELITE_DIRECT_WATCH_VERSION = 'lane3-elite-direct-watch-v2-20260918';
const LEGACY_VERSION = 'lane3-elite-direct-watch-v1-20260917';

export interface EliteDirectTarget {
  portfolioId: string;
  ownerId: string;
  username: string;
  sourceFilter: string;
}

export interface StoredTarget extends EliteDirectTarget {
  baselineAtMs: number;
  processedThroughMs: number;
  selectorInitialized: boolean;
  lastSelectorUpdatedAtMs: number | null;
  lastFallbackPollAtMs: number;
  closedHistoryInitialized: boolean;
  closedProcessedThroughMs: number;
  closedBoundaryIds: string[];
  lastClosedPollAtMs: number;
}

export interface DirectHydrationPlanItem {
  target: StoredTarget;
  selectorUpdatedAtMs: number | null;
  reason: 'periodic_direct_poll' | 'selector_change';
}

export interface ClosedHydrationPlanItem { target: StoredTarget; reason: 'closed_history_baseline' | 'periodic_closed_poll' }

export interface HydrationRunResult<T> {
  attempted: T[];
  failed: Array<{ item: T; error: unknown }>;
  skippedAfterRateLimit: T[];
  rateLimited: boolean;
}

/**
 * Runs bounded target work without allowing one target failure to starve its peers.
 * A 429 is different: callers must apply a global cooldown, so remaining work is
 * reported as skipped and retried on a later scan. `noteAttempt` runs before I/O,
 * which makes the deadline ordering rotate even when a target persistently fails.
 */
export async function runIsolatedHydrations<T>(
  items: T[],
  noteAttempt: (item: T) => void,
  hydrate: (item: T) => Promise<void>,
): Promise<HydrationRunResult<T>> {
  const result: HydrationRunResult<T> = {
    attempted: [], failed: [], skippedAfterRateLimit: [], rateLimited: false,
  };
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    noteAttempt(item);
    result.attempted.push(item);
    try {
      await hydrate(item);
    } catch (error: any) {
      result.failed.push({ item, error });
      if (error?.status === 429) {
        result.rateLimited = true;
        result.skippedAfterRateLimit = items.slice(index + 1);
        break;
      }
    }
  }
  return result;
}

/**
 * The captured endpoint is newest-first across pages. Equal timestamps may span
 * pages, so an unrelated row at T proves nothing. We may stop only after passing
 * below T, exhausting the endpoint, or encountering every identity in the stored
 * T boundary set. The latter relies on the captured stable newest-first ordering:
 * the prior boundary set marks the already-scanned suffix at T.
 */
export function closedBoundaryProof(
  pageRows: any[],
  processedThroughMs: number,
  boundaryIds: ReadonlySet<string>,
  encounteredBoundaryIds: Set<string>,
  pageSize: number,
): { reached: boolean; reason: 'older_timestamp' | 'stored_boundary_ids' | 'endpoint_exhausted' | null } {
  for (const row of pageRows) {
    const atMs = directSourceTimeMs(row?.closedAt) ?? directSourceTimeMs(row?.updatedAt);
    const id = String(row?.baseId ?? row?.id ?? '').trim();
    if (atMs != null && atMs < processedThroughMs) return { reached: true, reason: 'older_timestamp' };
    if (atMs === processedThroughMs && id && boundaryIds.has(id)) encounteredBoundaryIds.add(id);
  }
  if (boundaryIds.size > 0 && encounteredBoundaryIds.size === boundaryIds.size) {
    return { reached: true, reason: 'stored_boundary_ids' };
  }
  if (pageRows.length < pageSize) return { reached: true, reason: 'endpoint_exhausted' };
  return { reached: false, reason: null };
}

export interface ClosedBaselineResult {
  boundaryReached: boolean;
  boundaryReason: 'older_timestamp' | 'endpoint_exhausted' | null;
  boundaryTimestampMs: number;
  boundaryRows: any[];
  boundaryIds: string[];
  pagesFetched: number;
  overflow: boolean;
  orderingViolation: ClosedOrderingViolation | null;
}

export interface ClosedOrderingViolation {
  kind: 'missing_effective_close_time' | 'within_page_reversal' | 'cross_page_reversal';
  page: number;
  rowIndex: number;
  previousTimestampMs: number | null;
  timestampMs: number | null;
}

export interface ClosedPageOrderingResult {
  firstTimestampMs: number | null;
  lastTimestampMs: number | null;
  violation: ClosedOrderingViolation | null;
}

/** Validates the observed newest-first ordering without treating it as an API guarantee. */
export function validateClosedPageOrdering(
  rows: any[], page: number, priorPageLastTimestampMs: number | null,
): ClosedPageOrderingResult {
  let firstTimestampMs: number | null = null;
  let previousTimestampMs: number | null = null;
  for (let rowIndex = 0; rowIndex < rows.length; rowIndex += 1) {
    const row = rows[rowIndex];
    const timestampMs = directSourceTimeMs(row?.closedAt) ?? directSourceTimeMs(row?.updatedAt);
    if (timestampMs == null) {
      return { firstTimestampMs, lastTimestampMs: previousTimestampMs, violation: {
        kind: 'missing_effective_close_time', page, rowIndex,
        previousTimestampMs, timestampMs: null,
      } };
    }
    if (rowIndex === 0) {
      firstTimestampMs = timestampMs;
      if (priorPageLastTimestampMs != null && timestampMs > priorPageLastTimestampMs) {
        return { firstTimestampMs, lastTimestampMs: timestampMs, violation: {
          kind: 'cross_page_reversal', page, rowIndex,
          previousTimestampMs: priorPageLastTimestampMs, timestampMs,
        } };
      }
    } else if (previousTimestampMs != null && timestampMs > previousTimestampMs) {
      return { firstTimestampMs, lastTimestampMs: timestampMs, violation: {
        kind: 'within_page_reversal', page, rowIndex,
        previousTimestampMs, timestampMs,
      } };
    }
    previousTimestampMs = timestampMs;
  }
  return { firstTimestampMs, lastTimestampMs: previousTimestampMs, violation: null };
}

/**
 * Establishes a prospective CLOSED watermark around the newest visible timestamp.
 * Only that equal-timestamp group matters: older history is neither crawled nor
 * returned to the caller, and therefore can never become a replay candidate.
 */
export async function establishClosedBaseline(
  fetchPage: (page: number) => Promise<any[]>,
  maxPages: number,
  pageSize = 100,
): Promise<ClosedBaselineResult> {
  let boundaryTimestampMs = 0;
  const boundaryRows: any[] = [];
  const boundaryIds = new Set<string>();
  let pagesFetched = 0;
  let priorPageLastTimestampMs: number | null = null;

  for (let page = 1; page <= Math.max(1, maxPages); page += 1) {
    const rows = await fetchPage(page);
    pagesFetched += 1;
    const ordering = validateClosedPageOrdering(rows, page, priorPageLastTimestampMs);
    if (ordering.violation) {
      return { boundaryReached: false, boundaryReason: null, boundaryTimestampMs,
        boundaryRows, boundaryIds: [...boundaryIds].sort(), pagesFetched, overflow: true,
        orderingViolation: ordering.violation };
    }
    priorPageLastTimestampMs = ordering.lastTimestampMs ?? priorPageLastTimestampMs;
    const points = rows.flatMap(row => {
      const atMs = directSourceTimeMs(row?.closedAt) ?? directSourceTimeMs(row?.updatedAt);
      const id = String(row?.baseId ?? row?.id ?? '').trim();
      return atMs != null ? [{ row, atMs, id }] : [];
    });
    if (page === 1) {
      boundaryTimestampMs = points.reduce((newest, point) => Math.max(newest, point.atMs), 0);
      if (boundaryTimestampMs === 0) {
        if (rows.length < pageSize) {
          return { boundaryReached: true, boundaryReason: 'endpoint_exhausted', boundaryTimestampMs: 0,
            boundaryRows: [], boundaryIds: [], pagesFetched, overflow: false, orderingViolation: null };
        }
        return { boundaryReached: false, boundaryReason: null, boundaryTimestampMs: 0,
          boundaryRows: [], boundaryIds: [], pagesFetched, overflow: true, orderingViolation: null };
      }
    }
    for (const point of points) {
      if (point.atMs === boundaryTimestampMs) {
        boundaryRows.push(point.row);
        if (point.id) boundaryIds.add(point.id);
      }
    }
    if (points.some(point => point.atMs < boundaryTimestampMs)) {
      return { boundaryReached: true, boundaryReason: 'older_timestamp', boundaryTimestampMs,
        boundaryRows, boundaryIds: [...boundaryIds].sort(), pagesFetched, overflow: false, orderingViolation: null };
    }
    if (rows.length < pageSize) {
      return { boundaryReached: true, boundaryReason: 'endpoint_exhausted', boundaryTimestampMs,
        boundaryRows, boundaryIds: [...boundaryIds].sort(), pagesFetched, overflow: false, orderingViolation: null };
    }
  }
  return { boundaryReached: false, boundaryReason: null, boundaryTimestampMs,
    boundaryRows, boundaryIds: [...boundaryIds].sort(), pagesFetched, overflow: true, orderingViolation: null };
}

export function planDirectHydrations(
  targets: StoredTarget[],
  selectorChanges: ReadonlyMap<string, number>,
  nowMs: number,
  fallbackPollMs: number,
  maxHydrations: number,
): DirectHydrationPlanItem[] {
  return targets
    .flatMap(target => {
      const selectorUpdatedAtMs = selectorChanges.get(target.portfolioId) ?? null;
      const periodicDue = nowMs - target.lastFallbackPollAtMs >= fallbackPollMs;
      if (!periodicDue && selectorUpdatedAtMs == null) return [];
      return [{
        target,
        selectorUpdatedAtMs,
        reason: periodicDue ? 'periodic_direct_poll' as const : 'selector_change' as const,
      }];
    })
    .sort((a, b) => {
      if (a.reason !== b.reason) return a.reason === 'periodic_direct_poll' ? -1 : 1;
      if (a.target.lastFallbackPollAtMs !== b.target.lastFallbackPollAtMs) {
        return a.target.lastFallbackPollAtMs - b.target.lastFallbackPollAtMs;
      }
      return a.target.portfolioId.localeCompare(b.target.portfolioId);
    })
    .slice(0, Math.max(0, maxHydrations));
}

export function planClosedHydrations(
  targets: StoredTarget[], nowMs: number, closedPollMs: number, maxHydrations: number,
): ClosedHydrationPlanItem[] {
  return targets
    .filter(target => !target.closedHistoryInitialized || nowMs - target.lastClosedPollAtMs >= closedPollMs)
    .sort((a, b) => {
      if (a.closedHistoryInitialized !== b.closedHistoryInitialized) return a.closedHistoryInitialized ? 1 : -1;
      if (a.lastClosedPollAtMs !== b.lastClosedPollAtMs) return a.lastClosedPollAtMs - b.lastClosedPollAtMs;
      return a.portfolioId.localeCompare(b.portfolioId);
    })
    .slice(0, Math.max(0, maxHydrations))
    .map(target => ({ target, reason: target.closedHistoryInitialized ? 'periodic_closed_poll' : 'closed_history_baseline' }));
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
  closedInitializedCount: number;
  oldestClosedProcessedThroughMs: number | null;
  oldestOpenPollAtMs: number | null;
  oldestClosedPollAtMs: number | null;
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
      if ([ELITE_DIRECT_WATCH_VERSION, LEGACY_VERSION].includes(parsed?.version) && parsed?.targets && typeof parsed.targets === 'object') {
        this.state = { version: ELITE_DIRECT_WATCH_VERSION, targets: {} };
        for (const [portfolioId, raw] of Object.entries(parsed.targets)) {
          const target = raw as Partial<StoredTarget>;
          this.state.targets[portfolioId] = {
            ...(target as StoredTarget),
            closedHistoryInitialized: target.closedHistoryInitialized ?? false,
            closedProcessedThroughMs: target.closedProcessedThroughMs ?? 0,
            closedBoundaryIds: target.closedBoundaryIds ?? [],
            lastClosedPollAtMs: target.lastClosedPollAtMs ?? 0,
          };
        }
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
          closedHistoryInitialized: false,
          closedProcessedThroughMs: 0,
          closedBoundaryIds: [],
          lastClosedPollAtMs: 0,
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

  noteClosedPoll(portfolioId: string, atMs: number) {
    const target = this.state.targets[portfolioId];
    if (!target) return;
    target.lastClosedPollAtMs = atMs;
    this.save();
  }

  commitClosedHydration(portfolioId: string, rows: any[], polledAtMs: number) {
    const target = this.state.targets[portfolioId];
    if (!target) return;
    const points = rows.flatMap(row => {
      const atMs = directSourceTimeMs(row?.closedAt) ?? directSourceTimeMs(row?.updatedAt);
      const id = String(row?.baseId ?? row?.id ?? '').trim();
      return atMs != null && id ? [{ atMs, id }] : [];
    });
    const newestMs = points.reduce((value, point) => Math.max(value, point.atMs), target.closedProcessedThroughMs);
    const boundaryIds = new Set(
      newestMs === target.closedProcessedThroughMs ? target.closedBoundaryIds : [],
    );
    for (const point of points) if (point.atMs === newestMs) boundaryIds.add(point.id);
    target.closedHistoryInitialized = true;
    target.closedProcessedThroughMs = newestMs;
    target.closedBoundaryIds = [...boundaryIds].sort();
    target.lastClosedPollAtMs = polledAtMs;
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
    const closedProcessed = targets.filter(target => target.closedHistoryInitialized)
      .map(target => target.closedProcessedThroughMs).filter(Number.isFinite);
    return {
      version: ELITE_DIRECT_WATCH_VERSION,
      targetCount: targets.length,
      selectorInitializedCount: targets.filter(target => target.selectorInitialized).length,
      fallbackTargetCount: targets.filter(target => !target.selectorInitialized).length,
      oldestProcessedThroughMs: processed.length ? Math.min(...processed) : null,
      closedInitializedCount: targets.filter(target => target.closedHistoryInitialized).length,
      oldestClosedProcessedThroughMs: closedProcessed.length ? Math.min(...closedProcessed) : null,
      oldestOpenPollAtMs: targets.length ? Math.min(...targets.map(target => target.lastFallbackPollAtMs)) : null,
      oldestClosedPollAtMs: targets.length ? Math.min(...targets.map(target => target.lastClosedPollAtMs)) : null,
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
    openedSourceTimeMs: directSourceTimeMs(row?.createdAt),
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
    resultingSourceSize: action === 'increase' ? positive(row?.entrySize) : null,
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

export function closedSignalsAfterBoundary(
  closedRows: any[], target: EliteDirectTarget, processedThroughMs: number,
  boundaryIds: readonly string[], observedAtMs: number,
): InvoSignal[] {
  const atBoundary = new Set(boundaryIds);
  return signalsFromDirectInvestments([], closedRows, target, processedThroughMs - 1, observedAtMs)
    .filter(signal => {
      const sourceMs = signal.sourceTimeMs ?? 0;
      return sourceMs > processedThroughMs || (sourceMs === processedThroughMs && !atBoundary.has(signal.sourceBaseId));
    });
}

export function unownedCloseEvidence(signal: InvoSignal, observedOpen: boolean) {
  return {
    type: observedOpen ? 'close_ownership_gap' : 'missed_short_roundtrip',
    reason: observedOpen ? 'close_not_owned_by_service' : 'open_and_close_not_observed_while_open',
    lifecycleCopyability: observedOpen ? 'NOT_OWNED' : 'NON_COPYABLE_CLOSED_ONLY',
    reconstructedOpenExecuted: false,
    sourceCreatedAtMs: signal.openedSourceTimeMs ?? null,
    sourceClosedAtMs: signal.sourceTimeMs,
    portfolioId: signal.portfolioId,
  } as const;
}
