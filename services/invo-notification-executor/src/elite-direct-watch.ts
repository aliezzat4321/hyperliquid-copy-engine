import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs';
import { dirname } from 'path';
import type { InvoSignal } from './notification-signal.js';
import { ELITE_SELECTOR_VERSION, type PortfolioBucket } from './portfolio-candidates.js';

export const ELITE_DIRECT_WATCH_VERSION = 'lane3-elite-direct-watch-v4-20260918';
const LEGACY_VERSION = 'lane3-elite-direct-watch-v1-20260917';
const LEGACY_VERSION_2 = 'lane3-elite-direct-watch-v2-20260918';
const LEGACY_VERSION_3 = 'lane3-elite-direct-watch-v3-20260918';

export interface EliteDirectTarget {
  portfolioId: string;
  ownerId: string;
  username: string;
  sourceFilter: string;
}

export interface StoredTarget extends EliteDirectTarget {
  lifecycle?: 'ACTIVE' | 'RETIRING';
  retiredAtMs?: number | null;
  retireAfterMs?: number | null;
  lastRetirementOpenPollAtMs?: number;
  retirementRelevantOpenCount?: number | null;
  retirementOpenEmptyProofs?: number;
  retirementClosedProofs?: number;
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
  reason: 'periodic_direct_poll' | 'selector_change' | 'retirement_open_drain';
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
 * below T or exhausting the endpoint. Seeing stored identities is insufficient:
 * equal-timestamp rows have no stable secondary ordering and may continue later.
 */
export function closedBoundaryProof(
  pageRows: any[],
  processedThroughMs: number,
  boundaryIds: ReadonlySet<string>,
  encounteredBoundaryIds: Set<string>,
  pageSize: number,
): { reached: boolean; reason: 'older_timestamp' | 'endpoint_exhausted' | null } {
  for (const row of pageRows) {
    const atMs = directSourceTimeMs(row?.closedAt) ?? directSourceTimeMs(row?.updatedAt);
    const id = String(row?.baseId ?? row?.id ?? '').trim();
    if (atMs != null && atMs < processedThroughMs) return { reached: true, reason: 'older_timestamp' };
    if (atMs === processedThroughMs && id && boundaryIds.has(id)) encounteredBoundaryIds.add(id);
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

export type OpenOrderingViolation = ClosedOrderingViolation;
export interface OpenPaginationResult {
  rows: any[];
  pagesFetched: number;
  complete: boolean;
  overflow: boolean;
  orderingViolation: OpenOrderingViolation | null;
}

export type RetirementOpenDisposition =
  | { kind: 'execute'; signal: InvoSignal }
  | { kind: 'post_demotion_open_ignored'; signal: InvoSignal }
  | { kind: 'unowned_increase_ignored'; signal: InvoSignal };

/** Fetches the whole newest-first OPEN endpoint; no high-water is safe on overflow. */
export async function fetchCompleteOpenInvestments(
  fetchPage: (page: number) => Promise<any[]>, maxPages: number, pageSize = 100,
): Promise<OpenPaginationResult> {
  const rows: any[] = [];
  let priorLast: number | null = null;
  for (let page = 1; page <= Math.max(1, maxPages); page += 1) {
    const pageRows = await fetchPage(page);
    const normalized = pageRows.map(row => ({ ...row, closedAt: undefined }));
    const ordering = validateOpenPageOrdering(normalized, page, priorLast);
    if (ordering.violation) return { rows, pagesFetched: page, complete: false, overflow: true, orderingViolation: ordering.violation };
    priorLast = ordering.lastTimestampMs ?? priorLast;
    rows.push(...pageRows);
    if (pageRows.length < pageSize) return { rows, pagesFetched: page, complete: true, overflow: false, orderingViolation: null };
  }
  return { rows, pagesFetched: Math.max(1, maxPages), complete: false, overflow: true, orderingViolation: null };
}

function validateOpenPageOrdering(rows: any[], page: number, prior: number | null): ClosedPageOrderingResult {
  return validateTimestampOrdering(rows, page, prior, row => directSourceTimeMs(row?.updatedAt) ?? directSourceTimeMs(row?.createdAt));
}

function validateTimestampOrdering(
  rows: any[], page: number, priorPageLastTimestampMs: number | null,
  timestamp: (row: any) => number | null,
): ClosedPageOrderingResult {
  let firstTimestampMs: number | null = null;
  let previousTimestampMs: number | null = null;
  for (let rowIndex = 0; rowIndex < rows.length; rowIndex += 1) {
    const timestampMs = timestamp(rows[rowIndex]);
    if (timestampMs == null) return { firstTimestampMs, lastTimestampMs: previousTimestampMs, violation: {
      kind: 'missing_effective_close_time', page, rowIndex, previousTimestampMs, timestampMs: null,
    } };
    if (rowIndex === 0) {
      firstTimestampMs = timestampMs;
      if (priorPageLastTimestampMs != null && timestampMs > priorPageLastTimestampMs) return {
        firstTimestampMs, lastTimestampMs: timestampMs, violation: { kind: 'cross_page_reversal', page, rowIndex,
          previousTimestampMs: priorPageLastTimestampMs, timestampMs },
      };
    } else if (previousTimestampMs != null && timestampMs > previousTimestampMs) return {
      firstTimestampMs, lastTimestampMs: timestampMs, violation: { kind: 'within_page_reversal', page, rowIndex,
        previousTimestampMs, timestampMs },
    };
    previousTimestampMs = timestampMs;
  }
  return { firstTimestampMs, lastTimestampMs: previousTimestampMs, violation: null };
}

/** Validates the observed newest-first ordering without treating it as an API guarantee. */
export function validateClosedPageOrdering(
  rows: any[], page: number, priorPageLastTimestampMs: number | null,
): ClosedPageOrderingResult {
  return validateTimestampOrdering(rows, page, priorPageLastTimestampMs,
    row => directSourceTimeMs(row?.closedAt) ?? directSourceTimeMs(row?.updatedAt));
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
    .flatMap<DirectHydrationPlanItem>(target => {
      if (target.lifecycle === 'RETIRING') {
        if (nowMs - (target.lastRetirementOpenPollAtMs ?? 0) < fallbackPollMs) return [];
        return [{ target, selectorUpdatedAtMs: null, reason: 'retirement_open_drain' as const }];
      }
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
    .filter(target => nowMs - target.lastClosedPollAtMs >= closedPollMs)
    .sort((a, b) => a.lastClosedPollAtMs - b.lastClosedPollAtMs || a.portfolioId.localeCompare(b.portfolioId))
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
  retiringTargetCount: number;
  retirementRelevantOpenCount: number;
  retirementOpenEmptyProofCount: number;
  retirementClosedProofCount: number;
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

const PORTFOLIO_BUCKETS = new Set<PortfolioBucket>([
  'ELITE_CANDIDATE', 'SPARSE_HIGH_RETURN', 'RESEARCH_WIDE', 'REJECTED_DEMOTED',
]);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value == null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

export interface EliteDirectTargetLoadResult {
  targets: EliteDirectTarget[];
  demotedPortfolioIds: string[];
  observedAtMs: number | null;
  stale: boolean;
  validationError: string | null;
}

function rejectedTargets(observedAtMs: number | null, validationError: string): EliteDirectTargetLoadResult {
  return { targets: [], demotedPortfolioIds: [], observedAtMs, stale: true, validationError };
}

export function loadEliteDirectTargets(
  candidateStatePath: string,
  nowMs: number,
  maxAgeMs: number,
): EliteDirectTargetLoadResult {
  if (!existsSync(candidateStatePath)) return rejectedTargets(null, 'candidate_state_missing');
  let parsed: any;
  try { parsed = JSON.parse(readFileSync(candidateStatePath, 'utf8')); }
  catch { return rejectedTargets(null, 'candidate_state_unparseable'); }
  if (!isPlainObject(parsed)) return rejectedTargets(null, 'candidate_state_invalid_wrapper');
  const rawObservedAtMs = parsed.lastObservedAtMs;
  const reportedObservedAtMs = typeof rawObservedAtMs === 'number' && Number.isFinite(rawObservedAtMs)
    ? rawObservedAtMs : null;
  if (parsed.selectorVersion !== ELITE_SELECTOR_VERSION) {
    return rejectedTargets(reportedObservedAtMs, 'candidate_selector_version_mismatch');
  }
  if (typeof rawObservedAtMs !== 'number' || !Number.isFinite(rawObservedAtMs) || rawObservedAtMs <= 0) {
    return rejectedTargets(reportedObservedAtMs, 'candidate_state_timestamp_invalid');
  }
  const observedAtMs = rawObservedAtMs;
  if (observedAtMs > nowMs) return rejectedTargets(observedAtMs, 'candidate_state_from_future');
  if (nowMs - observedAtMs > maxAgeMs) return rejectedTargets(observedAtMs, 'candidate_state_stale');
  if (!isPlainObject(parsed.portfolios)) {
    return rejectedTargets(observedAtMs, 'candidate_portfolios_invalid');
  }
  const targets: EliteDirectTarget[] = [];
  const demotedPortfolioIds: string[] = [];
  for (const [key, row] of Object.entries(parsed.portfolios) as Array<[string, any]>) {
    const portfolioId = String(row?.portfolioId ?? '').trim();
    const rowObservedAtMs = row?.observedAtMs;
    if (!isPlainObject(row) || !portfolioId || key !== portfolioId
      || typeof rowObservedAtMs !== 'number' || !Number.isFinite(rowObservedAtMs) || rowObservedAtMs <= 0) {
      return rejectedTargets(observedAtMs, 'candidate_row_invalid');
    }
    if (rowObservedAtMs !== observedAtMs) continue;
    if (row.selectorVersion !== ELITE_SELECTOR_VERSION
      || !PORTFOLIO_BUCKETS.has(row.bucket as PortfolioBucket)) {
      return rejectedTargets(observedAtMs, 'candidate_fresh_row_invalid');
    }
    if (row?.bucket !== 'ELITE_CANDIDATE') { demotedPortfolioIds.push(portfolioId); continue; }
    const ownerId = String(row?.ownerId ?? '').trim();
    const username = normalizedUsername(row?.username);
    const sourceFilter = String(row?.sourceFilter ?? '').trim().toLowerCase();
    if (!ownerId || !username || !sourceFilter) {
      return rejectedTargets(observedAtMs, 'candidate_fresh_row_invalid');
    }
    targets.push({ portfolioId, ownerId, username, sourceFilter });
  }
  targets.sort((a, b) => a.portfolioId.localeCompare(b.portfolioId));
  return { targets, demotedPortfolioIds: [...new Set(demotedPortfolioIds)].sort(), observedAtMs,
    stale: false, validationError: null };
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
      if ([ELITE_DIRECT_WATCH_VERSION, LEGACY_VERSION_3, LEGACY_VERSION_2, LEGACY_VERSION].includes(parsed?.version) && parsed?.targets && typeof parsed.targets === 'object') {
        this.state = { version: ELITE_DIRECT_WATCH_VERSION, targets: {} };
        for (const [portfolioId, raw] of Object.entries(parsed.targets)) {
          const target = raw as Partial<StoredTarget>;
          this.state.targets[portfolioId] = {
            ...(target as StoredTarget),
            lifecycle: target.lifecycle ?? 'ACTIVE',
            retiredAtMs: target.retiredAtMs ?? null,
            retireAfterMs: target.retireAfterMs ?? null,
            lastRetirementOpenPollAtMs: target.lastRetirementOpenPollAtMs ?? 0,
            retirementRelevantOpenCount: target.retirementRelevantOpenCount ?? null,
            retirementOpenEmptyProofs: target.retirementOpenEmptyProofs ?? 0,
            retirementClosedProofs: target.retirementClosedProofs ?? 0,
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

  syncTargets(
    targets: EliteDirectTarget[], ownedPortfolioIds: Set<string>, baselineAtMs: number,
    authoritative = true, retirementGraceMs = 120_000, demotedPortfolioIds: ReadonlySet<string> = new Set(),
  ) {
    // Missing, malformed, or stale candidate state is not a demotion signal.
    if (!authoritative) return;
    let changed = false;
    for (const target of targets) {
      const existing = this.state.targets[target.portfolioId];
      let canonical = existing;
      if (!existing) {
        canonical = this.state.targets[target.portfolioId] = {
          ...target,
          lifecycle: 'ACTIVE', retiredAtMs: null, retireAfterMs: null,
          lastRetirementOpenPollAtMs: 0,
          retirementRelevantOpenCount: null, retirementOpenEmptyProofs: 0, retirementClosedProofs: 0,
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
        canonical = this.state.targets[target.portfolioId] = { ...existing, ...target };
        changed = true;
      }
      if (canonical && canonical.lifecycle !== 'ACTIVE') {
        Object.assign(canonical, { lifecycle: 'ACTIVE', retiredAtMs: null, retireAfterMs: null,
          lastRetirementOpenPollAtMs: 0,
          retirementRelevantOpenCount: null, retirementOpenEmptyProofs: 0, retirementClosedProofs: 0 });
        changed = true;
      }
    }
    for (const portfolioId of Object.keys(this.state.targets)) {
      const existing = this.state.targets[portfolioId];
      if (demotedPortfolioIds.has(portfolioId) && existing.lifecycle === 'ACTIVE') {
        existing.lifecycle = 'RETIRING';
        existing.retiredAtMs = baselineAtMs;
        existing.retireAfterMs = baselineAtMs + retirementGraceMs;
        existing.lastRetirementOpenPollAtMs = 0;
        existing.retirementRelevantOpenCount = null;
        existing.retirementOpenEmptyProofs = 0;
        existing.retirementClosedProofs = 0;
        changed = true;
      }
      if (!ownedPortfolioIds.has(portfolioId)
        && existing.lifecycle === 'RETIRING'
        && (existing.retirementOpenEmptyProofs ?? 0) >= 2
        && (existing.retirementClosedProofs ?? 0) >= 2
        && baselineAtMs >= (existing.retireAfterMs ?? Number.POSITIVE_INFINITY)
        && existing.closedHistoryInitialized) {
        delete this.state.targets[portfolioId]; changed = true;
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

  commitRetirementOpenPoll(portfolioId: string, rows: any[], polledAtMs: number) {
    const target = this.state.targets[portfolioId];
    if (!target || target.lifecycle !== 'RETIRING') return;
    const retiredAtMs = target.retiredAtMs ?? Number.NEGATIVE_INFINITY;
    const relevant = rows.filter(row => {
      const createdAtMs = directSourceTimeMs(row?.createdAt);
      return createdAtMs == null || createdAtMs <= retiredAtMs;
    });
    target.lastRetirementOpenPollAtMs = polledAtMs;
    target.lastFallbackPollAtMs = polledAtMs;
    target.retirementRelevantOpenCount = relevant.length;
    target.retirementOpenEmptyProofs = polledAtMs >= (target.retireAfterMs ?? Number.POSITIVE_INFINITY) && relevant.length === 0
      ? (target.retirementOpenEmptyProofs ?? 0) + 1 : 0;
    this.save();
  }

  noteClosedPoll(portfolioId: string, atMs: number) {
    const target = this.state.targets[portfolioId];
    if (!target) return;
    target.lastClosedPollAtMs = atMs;
    this.save();
  }

  initializeRetirementDrain(portfolioId: string) {
    const target = this.state.targets[portfolioId];
    if (!target || target.lifecycle !== 'RETIRING' || target.closedHistoryInitialized) return;
    target.closedHistoryInitialized = true;
    target.closedProcessedThroughMs = target.baselineAtMs;
    target.closedBoundaryIds = [];
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
    if (target.lifecycle === 'RETIRING' && polledAtMs >= (target.retireAfterMs ?? Number.POSITIVE_INFINITY)) {
      target.retirementClosedProofs = (target.retirementClosedProofs ?? 0) + 1;
    }
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
      retiringTargetCount: targets.filter(target => target.lifecycle === 'RETIRING').length,
      retirementRelevantOpenCount: targets.reduce((sum, target) => sum + (target.retirementRelevantOpenCount ?? 0), 0),
      retirementOpenEmptyProofCount: targets.reduce((sum, target) => sum + (target.retirementOpenEmptyProofs ?? 0), 0),
      retirementClosedProofCount: targets.reduce((sum, target) => sum + (target.retirementClosedProofs ?? 0), 0),
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

/** Classifies RETIRING open rows without weakening normal execution gates. */
export function retiringOpenDispositions(
  openRows: any[], target: StoredTarget, observedAtMs: number,
  isManagedSource: (sourceBaseId: string) => boolean,
): RetirementOpenDisposition[] {
  const retiredAtMs = target.retiredAtMs ?? Number.NEGATIVE_INFINITY;
  return signalsFromDirectInvestments(
    openRows, [], target, target.processedThroughMs, observedAtMs,
  ).map(signal => {
    const lifecycleCreatedAtMs = signal.openedSourceTimeMs;
    if (lifecycleCreatedAtMs == null || lifecycleCreatedAtMs > retiredAtMs) {
      return { kind: 'post_demotion_open_ignored', signal };
    }
    if (signal.action === 'increase' && !isManagedSource(signal.sourceBaseId)) {
      return { kind: 'unowned_increase_ignored', signal };
    }
    return { kind: 'execute', signal };
  });
}

export function isMissedPreDemotionOpen(
  signal: InvoSignal, wakeSource: string, decisionAtMs: number, maxSignalAgeMs: number,
): boolean {
  return wakeSource === 'elite_direct:retirement_open_drain'
    && signal.action === 'open'
    && signal.sourceTimeMs != null
    && decisionAtMs - signal.sourceTimeMs > maxSignalAgeMs;
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
