import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, statSync, truncateSync, writeFileSync } from 'fs';
import { dirname } from 'path';
import type { InvoSignal } from './notification-signal.js';
import { ELITE_SELECTOR_VERSION, type PortfolioBucket } from './portfolio-candidates.js';
import { signalWasSeen } from './source-event-dedupe.js';

export const ELITE_DIRECT_WATCH_VERSION = 'lane3-elite-direct-watch-v6-20260920';
const LEGACY_VERSION_5 = 'lane3-elite-direct-watch-v5-20260920';
const LEGACY_VERSION_4 = 'lane3-elite-direct-watch-v4-20260918';
const LEGACY_VERSION = 'lane3-elite-direct-watch-v1-20260917';
const LEGACY_VERSION_2 = 'lane3-elite-direct-watch-v2-20260918';
const LEGACY_VERSION_3 = 'lane3-elite-direct-watch-v3-20260918';

export interface EliteDirectTarget {
  portfolioId: string;
  ownerId: string;
  username: string;
  sourceFilter: string;
  score: number;
}

export interface StoredTarget extends EliteDirectTarget {
  lifecycle?: 'ENROLLING' | 'ACTIVE' | 'MISSING_GRACE' | 'RETIRING';
  admittedAtMs?: number | null;
  openHistoryInitialized?: boolean;
  firstNegativeAtMs?: number | null;
  lastNegativeAtMs?: number | null;
  negativeEvidenceCount?: number;
  negativeSelectorVersion?: string | null;
  negativeEvidenceReason?: string | null;
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

export interface Tombstone extends EliteDirectTarget {
  lifecycle: 'TOMBSTONE';
  tombstonedAtMs: number;
  baselineAtMs: number;
  processedThroughMs: number;
  closedHistoryInitialized: boolean;
  closedProcessedThroughMs: number;
  closedBoundaryIds: string[];
  selectorInitialized: boolean;
  lastSelectorUpdatedAtMs: number | null;
  lastNegativeAtMs: number | null;
  negativeSelectorVersion: string | null;
  negativeEvidenceReason: string | null;
}

export interface DeferredAdmission {
  portfolioId: string;
  deferredAtMs: number;
  selectorVersion: string;
  reason: 'resident_capacity_full' | 'capacity_unhealthy';
  cap: number;
  score: number;
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
  | { kind: 'pre_selection_open_ignored'; signal: InvoSignal }
  | { kind: 'post_demotion_open_ignored'; signal: InvoSignal }
  | { kind: 'unowned_increase_ignored'; signal: InvoSignal }
  | { kind: 'invalid_lifecycle_open'; evidenceKey: string; sourceBaseId: string | null; createdAt: unknown };

export interface RetirementLifecycleState {
  hasObservedOpen: (sourceBaseId: string) => boolean;
  isManagedSource: (sourceBaseId: string) => boolean;
  hasHandledClose: (sourceBaseId: string) => boolean;
  hasSeen: (key: string) => boolean;
}

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
      // Establish the CLOSED cursor before copying any OPEN. Otherwise a source
      // position can be copied open and then swallowed by its first CLOSED baseline.
      if (!target.closedHistoryInitialized) return [];
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
  tombstones: Record<string, Tombstone>;
  deferredAdmissions: Record<string, DeferredAdmission>;
}

interface AdmissionIndexRow {
  portfolioId: string;
  admittedAtMs: number;
  score: number;
  selectorVersion: string;
}

interface AdmissionIndexDiskState {
  version: 1;
  generatedAtMs: number;
  rows: Record<string, AdmissionIndexRow>;
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
  activeTargetCount: number;
  enrollingTargetCount: number;
  missingGraceTargetCount: number;
  tombstoneCount: number;
  deferredAdmissionCount: number;
  durableStateCardinality: number;
  serializedStateBytes: number;
  retirementRelevantOpenCount: number;
  retirementOpenEmptyProofCount: number;
  retirementClosedProofCount: number;
}

export interface DirectWatchCapacity {
  sustainableOpenTargetCeiling: number;
  sustainableClosedTargetCeiling: number;
  worstCaseOpenSweepMsAtCap: number;
  worstCaseClosedSweepMsAtCap: number;
  provenResidentCap: number;
  worstCaseRequestBudget: number;
  fixedOverheadMs: number;
}

export function directWatchCapacity(input: {
  residentCap: number;
  scanMs: number;
  maxOpenHydratesPerScan: number;
  openPollMs: number;
  maxClosedHydratesPerScan: number;
  closedPollMs: number;
  requestTimeoutMs: number;
  openMaxPages: number;
  closedMaxPages: number;
  fixedOverheadMs: number;
}): DirectWatchCapacity {
  const openDeadlineBudgetMs = Math.max(0, input.openPollMs - input.fixedOverheadMs);
  const closedDeadlineBudgetMs = Math.max(0, input.closedPollMs - input.fixedOverheadMs);
  const openTargetCostMs = input.openMaxPages * input.requestTimeoutMs;
  const closedTargetCostMs = input.closedMaxPages * input.requestTimeoutMs;
  const sustainableOpenTargetCeiling = Math.min(
    input.maxOpenHydratesPerScan,
    openTargetCostMs > 0 ? Math.floor(openDeadlineBudgetMs / openTargetCostMs) : 0,
  );
  const sustainableClosedTargetCeiling = Math.min(
    input.maxClosedHydratesPerScan,
    closedTargetCostMs > 0 ? Math.floor(closedDeadlineBudgetMs / closedTargetCostMs) : 0,
  );
  const provenResidentCap = Math.max(0, Math.min(
    input.residentCap, sustainableOpenTargetCeiling, sustainableClosedTargetCeiling,
  ));
  return {
    sustainableOpenTargetCeiling,
    sustainableClosedTargetCeiling,
    worstCaseOpenSweepMsAtCap: input.fixedOverheadMs + provenResidentCap * openTargetCostMs,
    worstCaseClosedSweepMsAtCap: input.fixedOverheadMs + provenResidentCap * closedTargetCostMs,
    provenResidentCap,
    worstCaseRequestBudget: provenResidentCap * (input.openMaxPages + input.closedMaxPages),
    fixedOverheadMs: input.fixedOverheadMs,
  };
}

export function validateDirectWatchCapacity(input: Parameters<typeof directWatchCapacity>[0]): DirectWatchCapacity {
  const capacity = directWatchCapacity(input);
  if (capacity.provenResidentCap < 1) throw new Error(
    'direct-watch request budget cannot prove even one resident target within OPEN and CLOSED deadlines',
  );
  return capacity;
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
    if (rowObservedAtMs > observedAtMs || nowMs - rowObservedAtMs > maxAgeMs) continue;
    if (row.selectorVersion !== ELITE_SELECTOR_VERSION
      || !PORTFOLIO_BUCKETS.has(row.bucket as PortfolioBucket)) {
      return rejectedTargets(observedAtMs, 'candidate_fresh_row_invalid');
    }
    if (row?.bucket !== 'ELITE_CANDIDATE') { demotedPortfolioIds.push(portfolioId); continue; }
    const ownerId = String(row?.ownerId ?? '').trim();
    const username = normalizedUsername(row?.username);
    const sourceFilter = String(row?.sourceFilter ?? '').trim().toLowerCase();
    const score = Number(row?.score);
    if (!ownerId || !username || !sourceFilter || !Number.isFinite(score)) {
      return rejectedTargets(observedAtMs, 'candidate_fresh_row_invalid');
    }
    targets.push({ portfolioId, ownerId, username, sourceFilter, score });
  }
  targets.sort((a, b) => b.score - a.score || a.portfolioId.localeCompare(b.portfolioId));
  return { targets, demotedPortfolioIds: [...new Set(demotedPortfolioIds)].sort(), observedAtMs,
    stale: false, validationError: null };
}

export class EliteDirectWatchState {
  private state: DirectWatchDiskState = {
    version: ELITE_DIRECT_WATCH_VERSION, targets: {}, tombstones: {}, deferredAdmissions: {},
  };

  private readonly journalPath: string;
  readonly admissionIndexPath: string;
  private readonly maxRetainedTombstones = 256;
  private readonly maxDeferredAdmissions = 256;
  private readonly maxJournalBytes = 1024 * 1024;
  private enforcedResidentCap = Number.POSITIVE_INFINITY;
  private admissionsEnabled = true;

  constructor(private readonly path: string, admissionIndexPath = `${path}.admissions.json`) {
    this.journalPath = `${path}.journal.jsonl`;
    this.admissionIndexPath = admissionIndexPath;
    this.load();
  }

  private load() {
    if (!existsSync(this.path)) {
      this.writeAdmissionIndex();
      return;
    }
    try {
      const parsed = JSON.parse(readFileSync(this.path, 'utf8')) as DirectWatchDiskState;
      if (![ELITE_DIRECT_WATCH_VERSION, LEGACY_VERSION_5, LEGACY_VERSION_4, LEGACY_VERSION_3, LEGACY_VERSION_2, LEGACY_VERSION].includes(parsed?.version)) {
        throw new Error(`unsupported direct-watch state version: ${String(parsed?.version)}`);
      }
      if (!isPlainObject(parsed?.targets) || (parsed.tombstones != null && !isPlainObject(parsed.tombstones))
        || (parsed.deferredAdmissions != null && !isPlainObject(parsed.deferredAdmissions))) {
        throw new Error('invalid direct-watch state wrapper');
      }
      {
        this.state = { version: ELITE_DIRECT_WATCH_VERSION, targets: {},
          tombstones: parsed.tombstones ?? {}, deferredAdmissions: parsed.deferredAdmissions ?? {} };
        for (const [portfolioId, raw] of Object.entries(this.state.tombstones)) {
          if (!isPlainObject(raw) || raw.portfolioId !== portfolioId || raw.lifecycle !== 'TOMBSTONE'
            || !Number.isFinite(raw.processedThroughMs) || !Number.isFinite(raw.closedProcessedThroughMs)) {
            throw new Error(`invalid direct-watch tombstone: ${portfolioId}`);
          }
          raw.score = Number.isFinite(raw.score) ? Number(raw.score) : 0;
        }
        for (const [portfolioId, raw] of Object.entries(this.state.deferredAdmissions)) {
          if (!isPlainObject(raw) || raw.portfolioId !== portfolioId || !Number.isFinite(raw.deferredAtMs)) {
            throw new Error(`invalid direct-watch deferred admission: ${portfolioId}`);
          }
          raw.score = Number.isFinite(raw.score) ? Number(raw.score) : 0;
        }
        for (const [portfolioId, raw] of Object.entries(parsed.targets)) {
          const target = raw as Partial<StoredTarget>;
          if (!isPlainObject(raw) || target.portfolioId !== portfolioId || !target.ownerId || !target.username
            || !target.sourceFilter || !Number.isFinite(target.baselineAtMs)
            || !Number.isFinite(target.processedThroughMs)) {
            throw new Error(`invalid direct-watch target: ${portfolioId}`);
          }
          const migratedReady = parsed.version === ELITE_DIRECT_WATCH_VERSION
            && target.openHistoryInitialized === true && target.closedHistoryInitialized === true
            && Number.isFinite(target.admittedAtMs);
          this.state.targets[portfolioId] = {
            ...(target as StoredTarget),
            score: Number.isFinite(target.score) ? Number(target.score) : 0,
            lifecycle: migratedReady ? target.lifecycle ?? 'ACTIVE' : 'ENROLLING',
            admittedAtMs: migratedReady ? target.admittedAtMs ?? null : null,
            openHistoryInitialized: migratedReady,
            firstNegativeAtMs: target.firstNegativeAtMs ?? null,
            lastNegativeAtMs: target.lastNegativeAtMs ?? null,
            negativeEvidenceCount: target.negativeEvidenceCount ?? 0,
            negativeSelectorVersion: target.negativeSelectorVersion ?? null,
            negativeEvidenceReason: target.negativeEvidenceReason ?? null,
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
        this.replayJournal();
        this.compactAuxiliaryState(Date.now(), new Set(Object.keys(this.state.targets)));
        this.save();
      }
    } catch (error) {
      throw new Error(`direct-watch state load failed closed: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  private replayJournal() {
    if (!existsSync(this.journalPath)) return;
    const text = readFileSync(this.journalPath, 'utf8');
    for (const [index, line] of text.split('\n').entries()) {
      if (!line.trim()) continue;
      const entry = JSON.parse(line);
      if (!isPlainObject(entry) || entry.version !== 1 || typeof entry.portfolioId !== 'string'
        || !isPlainObject(entry.target) || entry.target.portfolioId !== entry.portfolioId) {
        throw new Error(`invalid direct-watch journal row ${index + 1}`);
      }
      if (this.state.targets[entry.portfolioId]) this.state.targets[entry.portfolioId] = entry.target as unknown as StoredTarget;
    }
  }

  private save() {
    mkdirSync(dirname(this.path), { recursive: true });
    const temp = `${this.path}.tmp`;
    writeFileSync(temp, JSON.stringify(this.state, null, 2));
    renameSync(temp, this.path);
    if (existsSync(this.journalPath)) truncateSync(this.journalPath, 0);
    this.writeAdmissionIndex();
  }

  private persistTarget(target: StoredTarget) {
    mkdirSync(dirname(this.path), { recursive: true });
    appendFileSync(this.journalPath, `${JSON.stringify({ version: 1, portfolioId: target.portfolioId, target })}\n`);
    this.writeAdmissionIndex();
    if (statSync(this.journalPath).size >= this.maxJournalBytes) this.save();
  }

  private writeAdmissionIndex() {
    const rows: Record<string, AdmissionIndexRow> = {};
    const capacityHealthy = Object.keys(this.state.targets).length <= this.enforcedResidentCap;
    for (const target of capacityHealthy ? Object.values(this.state.targets) : []) {
      if (target.lifecycle !== 'ACTIVE' || !target.openHistoryInitialized || !target.closedHistoryInitialized
        || !Number.isFinite(target.admittedAtMs)) continue;
      rows[target.portfolioId] = { portfolioId: target.portfolioId,
        admittedAtMs: target.admittedAtMs as number, score: target.score,
        selectorVersion: ELITE_SELECTOR_VERSION };
    }
    mkdirSync(dirname(this.admissionIndexPath), { recursive: true });
    const temp = `${this.admissionIndexPath}.tmp`;
    const index: AdmissionIndexDiskState = { version: 1, generatedAtMs: Date.now(), rows };
    writeFileSync(temp, JSON.stringify(index));
    renameSync(temp, this.admissionIndexPath);
  }

  private compactAuxiliaryState(nowMs: number, qualifiedIds: ReadonlySet<string>): boolean {
    let changed = false;
    for (const portfolioId of Object.keys(this.state.deferredAdmissions)) {
      if (!qualifiedIds.has(portfolioId)) { delete this.state.deferredAdmissions[portfolioId]; changed = true; }
    }
    const deferred = Object.values(this.state.deferredAdmissions)
      .sort((a, b) => b.score - a.score || b.deferredAtMs - a.deferredAtMs || a.portfolioId.localeCompare(b.portfolioId));
    for (const row of deferred.slice(this.maxDeferredAdmissions)) {
      delete this.state.deferredAdmissions[row.portfolioId]; changed = true;
    }
    const tombstones = Object.values(this.state.tombstones)
      .sort((a, b) => b.tombstonedAtMs - a.tombstonedAtMs || a.portfolioId.localeCompare(b.portfolioId));
    for (const row of tombstones.slice(this.maxRetainedTombstones)) {
      delete this.state.tombstones[row.portfolioId]; changed = true;
    }
    void nowMs;
    return changed;
  }

  syncTargets(
    targets: EliteDirectTarget[], ownedPortfolioIds: Set<string>, baselineAtMs: number,
    authoritative = true, retirementGraceMs = 120_000, demotedPortfolioIds: ReadonlySet<string> = new Set(),
    residentCap = Number.POSITIVE_INFINITY, minimumNegativeObservations = 2,
    negativeGraceMs = 600_000, negativeObservedAtMs = baselineAtMs,
    negativeSelectorVersion = ELITE_SELECTOR_VERSION,
    admissionsHealthy = true,
  ) {
    // Missing, malformed, or stale candidate state is not a demotion signal.
    if (!authoritative) return;
    this.enforcedResidentCap = residentCap;
    this.admissionsEnabled = admissionsHealthy;
    let changed = false;
    const rankedTargets = [...targets].sort((a, b) => b.score - a.score || a.portfolioId.localeCompare(b.portfolioId));
    const qualifiedIds = new Set(rankedTargets.map(target => target.portfolioId));
    const qualityAdmitIds = new Set(rankedTargets.slice(0, Math.max(0, residentCap)).map(target => target.portfolioId));
    changed = this.compactAuxiliaryState(baselineAtMs, qualifiedIds) || changed;
    for (const portfolioId of demotedPortfolioIds) {
      if (this.state.deferredAdmissions[portfolioId]) { delete this.state.deferredAdmissions[portfolioId]; changed = true; }
    }

    const overflowCount = Math.max(0, Object.keys(this.state.targets).length - residentCap);
    if (overflowCount > 0) {
      const overflowVictims = Object.values(this.state.targets)
        .filter(row => row.lifecycle !== 'RETIRING' && !ownedPortfolioIds.has(row.portfolioId))
        .sort((a, b) => a.score - b.score || b.portfolioId.localeCompare(a.portfolioId))
        .slice(0, overflowCount);
      for (const victim of overflowVictims) {
        Object.assign(victim, { lifecycle: 'RETIRING', admittedAtMs: null,
          retiredAtMs: baselineAtMs, retireAfterMs: baselineAtMs + retirementGraceMs,
          lastRetirementOpenPollAtMs: 0, retirementRelevantOpenCount: null,
          retirementOpenEmptyProofs: 0, retirementClosedProofs: 0,
          negativeEvidenceReason: 'capacity_budget_reduction', negativeSelectorVersion,
          lastNegativeAtMs: negativeObservedAtMs });
        changed = true;
      }
    }

    // Quality displacement is a drain, never an eviction. A protected incumbent
    // remains resident until OPEN/CLOSED drain proofs make deletion safe, and the
    // replacement stays explicitly waitlisted meanwhile.
    if (Object.keys(this.state.targets).length >= residentCap) {
      const bestWaiting = rankedTargets.find(target => !this.state.targets[target.portfolioId]);
      const worstDisplaceable = Object.values(this.state.targets)
        .filter(row => row.lifecycle !== 'RETIRING' && !ownedPortfolioIds.has(row.portfolioId))
        .sort((a, b) => a.score - b.score || b.portfolioId.localeCompare(a.portfolioId))[0];
      if (bestWaiting && worstDisplaceable
        && (bestWaiting.score > worstDisplaceable.score
          || (bestWaiting.score === worstDisplaceable.score
            && bestWaiting.portfolioId.localeCompare(worstDisplaceable.portfolioId) < 0))) {
        Object.assign(worstDisplaceable, {
          lifecycle: 'RETIRING', admittedAtMs: null, retiredAtMs: baselineAtMs,
          retireAfterMs: baselineAtMs + retirementGraceMs, lastRetirementOpenPollAtMs: 0,
          retirementRelevantOpenCount: null, retirementOpenEmptyProofs: 0, retirementClosedProofs: 0,
          negativeEvidenceReason: 'capacity_quality_displacement', negativeSelectorVersion,
          lastNegativeAtMs: negativeObservedAtMs,
        });
        changed = true;
      }
    }

    for (const target of rankedTargets) {
      const existing = this.state.targets[target.portfolioId];
      let canonical = existing;
      if (!existing) {
        if (!this.admissionsEnabled || Object.keys(this.state.targets).length >= residentCap) {
          const prior = this.state.deferredAdmissions[target.portfolioId];
          const next: DeferredAdmission = {
            portfolioId: target.portfolioId, deferredAtMs: baselineAtMs,
            selectorVersion: negativeSelectorVersion,
            reason: this.admissionsEnabled ? 'resident_capacity_full' : 'capacity_unhealthy', cap: residentCap,
            score: target.score,
          };
          if (!prior || prior.score !== next.score || prior.cap !== next.cap
            || prior.selectorVersion !== next.selectorVersion || prior.reason !== next.reason) {
            this.state.deferredAdmissions[target.portfolioId] = next;
            changed = true;
          }
          continue;
        }
        const tombstone = this.state.tombstones[target.portfolioId];
        const restoredClosedThroughMs = tombstone
          ? Math.max(baselineAtMs, tombstone.closedProcessedThroughMs) : 0;
        canonical = this.state.targets[target.portfolioId] = {
          ...target,
          lifecycle: 'ENROLLING', admittedAtMs: null, openHistoryInitialized: false,
          retiredAtMs: null, retireAfterMs: null,
          firstNegativeAtMs: null, lastNegativeAtMs: null, negativeEvidenceCount: 0,
          negativeSelectorVersion: null, negativeEvidenceReason: null,
          lastRetirementOpenPollAtMs: 0,
          retirementRelevantOpenCount: null, retirementOpenEmptyProofs: 0, retirementClosedProofs: 0,
          baselineAtMs: tombstone?.baselineAtMs ?? baselineAtMs,
          processedThroughMs: Math.max(baselineAtMs, tombstone?.processedThroughMs ?? 0),
          selectorInitialized: tombstone?.selectorInitialized ?? false,
          lastSelectorUpdatedAtMs: tombstone?.lastSelectorUpdatedAtMs ?? null,
          lastFallbackPollAtMs: 0,
          closedHistoryInitialized: tombstone?.closedHistoryInitialized ?? false,
          closedProcessedThroughMs: restoredClosedThroughMs,
          closedBoundaryIds: tombstone && restoredClosedThroughMs === tombstone.closedProcessedThroughMs
            ? tombstone.closedBoundaryIds : [],
          lastClosedPollAtMs: 0,
        };
        delete this.state.tombstones[target.portfolioId];
        delete this.state.deferredAdmissions[target.portfolioId];
        changed = true;
      } else if (
        existing.ownerId !== target.ownerId
        || existing.username !== target.username
        || existing.sourceFilter !== target.sourceFilter || existing.score !== target.score
      ) {
        canonical = this.state.targets[target.portfolioId] = { ...existing, ...target };
        changed = true;
      }
      const qualityDisplaced = ['capacity_quality_displacement', 'capacity_budget_reduction']
        .includes(canonical?.negativeEvidenceReason ?? '')
        && !qualityAdmitIds.has(target.portfolioId);
      if (canonical && canonical.lifecycle !== 'ACTIVE' && canonical.lifecycle !== 'ENROLLING' && !qualityDisplaced) {
        Object.assign(canonical, { lifecycle: 'ENROLLING', admittedAtMs: null,
          openHistoryInitialized: false, retiredAtMs: null, retireAfterMs: null,
          firstNegativeAtMs: null, lastNegativeAtMs: null, negativeEvidenceCount: 0,
          negativeSelectorVersion: null, negativeEvidenceReason: null,
          lastRetirementOpenPollAtMs: 0,
          retirementRelevantOpenCount: null, retirementOpenEmptyProofs: 0, retirementClosedProofs: 0 });
        changed = true;
      }
    }
    for (const portfolioId of Object.keys(this.state.targets)) {
      const existing = this.state.targets[portfolioId];
      if (demotedPortfolioIds.has(portfolioId) && existing.lifecycle !== 'RETIRING'
        && negativeObservedAtMs !== existing.lastNegativeAtMs) {
        existing.firstNegativeAtMs ??= negativeObservedAtMs;
        existing.lastNegativeAtMs = negativeObservedAtMs;
        existing.negativeEvidenceCount = (existing.negativeEvidenceCount ?? 0) + 1;
        existing.negativeSelectorVersion = negativeSelectorVersion;
        existing.negativeEvidenceReason = 'fresh_explicit_non_elite_candidate_row';
        if ((existing.negativeEvidenceCount ?? 0) >= minimumNegativeObservations
          && negativeObservedAtMs - (existing.firstNegativeAtMs ?? negativeObservedAtMs) >= negativeGraceMs) {
          existing.lifecycle = 'RETIRING';
          existing.retiredAtMs = baselineAtMs;
          existing.retireAfterMs = baselineAtMs + retirementGraceMs;
          existing.lastRetirementOpenPollAtMs = 0;
          existing.retirementRelevantOpenCount = null;
          existing.retirementOpenEmptyProofs = 0;
          existing.retirementClosedProofs = 0;
        } else {
          existing.lifecycle = 'MISSING_GRACE';
          existing.admittedAtMs = null;
        }
        changed = true;
      }
      if (!ownedPortfolioIds.has(portfolioId)
        && existing.lifecycle === 'RETIRING'
        && (existing.retirementOpenEmptyProofs ?? 0) >= 2
        && (existing.retirementClosedProofs ?? 0) >= 2
        && baselineAtMs >= (existing.retireAfterMs ?? Number.POSITIVE_INFINITY)
        && existing.closedHistoryInitialized) {
        this.state.tombstones[portfolioId] = {
          portfolioId, ownerId: existing.ownerId, username: existing.username, score: existing.score,
          sourceFilter: existing.sourceFilter, lifecycle: 'TOMBSTONE', tombstonedAtMs: baselineAtMs,
          baselineAtMs: existing.baselineAtMs, processedThroughMs: existing.processedThroughMs,
          closedHistoryInitialized: existing.closedHistoryInitialized,
          closedProcessedThroughMs: existing.closedProcessedThroughMs,
          closedBoundaryIds: [...existing.closedBoundaryIds], selectorInitialized: existing.selectorInitialized,
          lastSelectorUpdatedAtMs: existing.lastSelectorUpdatedAtMs,
          lastNegativeAtMs: existing.lastNegativeAtMs ?? null,
          negativeSelectorVersion: existing.negativeSelectorVersion ?? null,
          negativeEvidenceReason: existing.negativeEvidenceReason ?? null,
        };
        delete this.state.targets[portfolioId]; changed = true;
      }
    }
    changed = this.compactAuxiliaryState(baselineAtMs, qualifiedIds) || changed;
    if (changed) this.save();
  }

  targets(): StoredTarget[] {
    return Object.values(this.state.targets).map(target => ({ ...target }));
  }

  tombstones(): Tombstone[] { return Object.values(this.state.tombstones).map(row => ({ ...row })); }

  deferredAdmissions(): DeferredAdmission[] {
    return Object.values(this.state.deferredAdmissions).map(row => ({ ...row }));
  }

  assertResidentCap(cap: number) {
    const count = Object.keys(this.state.targets).length;
    if (count > cap) throw new Error(`resident direct-watch state has ${count} targets, exceeding configured cap ${cap}`);
  }

  observeSelector(portfolioId: string, selectorUpdatedAtMs: number): { hydrate: boolean; processedThroughMs: number } {
    const target = this.state.targets[portfolioId];
    if (!target) return { hydrate: false, processedThroughMs: 0 };
    if (!target.selectorInitialized) {
      target.selectorInitialized = true;
      target.lastSelectorUpdatedAtMs = selectorUpdatedAtMs;
      this.persistTarget(target);
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
    // Scheduling fairness is executor-local and need not rewrite causal state.
  }

  commitRetirementOpenPoll(portfolioId: string, rows: any[], polledAtMs: number) {
    const target = this.state.targets[portfolioId];
    if (!target || target.lifecycle !== 'RETIRING') return;
    const retiredAtMs = target.retiredAtMs ?? Number.NEGATIVE_INFINITY;
    const relevant = rows.filter(row => {
      const createdAtMs = directSourceTimeMs(row?.createdAt);
      // Unknown lifecycle time fails closed and prevents a false empty proof.
      return createdAtMs == null || (createdAtMs >= target.baselineAtMs && createdAtMs <= retiredAtMs);
    });
    target.lastRetirementOpenPollAtMs = polledAtMs;
    target.lastFallbackPollAtMs = polledAtMs;
    target.retirementRelevantOpenCount = relevant.length;
    target.retirementOpenEmptyProofs = polledAtMs >= (target.retireAfterMs ?? Number.POSITIVE_INFINITY) && relevant.length === 0
      ? (target.retirementOpenEmptyProofs ?? 0) + 1 : 0;
    this.persistTarget(target);
  }

  noteClosedPoll(portfolioId: string, atMs: number) {
    const target = this.state.targets[portfolioId];
    if (!target) return;
    target.lastClosedPollAtMs = atMs;
    // Scheduling fairness is executor-local and need not rewrite causal state.
  }

  initializeRetirementDrain(portfolioId: string) {
    const target = this.state.targets[portfolioId];
    if (!target || target.lifecycle !== 'RETIRING' || target.closedHistoryInitialized) return;
    target.closedHistoryInitialized = true;
    target.closedProcessedThroughMs = target.baselineAtMs;
    target.closedBoundaryIds = [];
    this.persistTarget(target);
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
    this.persistTarget(target);
  }

  commitOpenBaseline(portfolioId: string, rows: any[], polledAtMs: number) {
    const target = this.state.targets[portfolioId];
    if (!target || target.lifecycle !== 'ENROLLING' || !target.closedHistoryInitialized
      || !this.admissionsEnabled || Object.keys(this.state.targets).length > this.enforcedResidentCap) return;
    const newest = rows.reduce((value, row) => Math.max(value,
      directSourceTimeMs(row?.updatedAt) ?? directSourceTimeMs(row?.createdAt) ?? 0), target.processedThroughMs);
    target.processedThroughMs = Math.max(newest, polledAtMs);
    target.openHistoryInitialized = true;
    target.admittedAtMs = polledAtMs;
    target.lifecycle = 'ACTIVE';
    target.lastFallbackPollAtMs = polledAtMs;
    this.persistTarget(target);
  }

  commitHydration(portfolioId: string, processedThroughMs: number, selectorUpdatedAtMs?: number) {
    const target = this.state.targets[portfolioId];
    if (!target) return;
    target.processedThroughMs = Math.max(target.processedThroughMs, processedThroughMs);
    if (selectorUpdatedAtMs != null) {
      target.selectorInitialized = true;
      target.lastSelectorUpdatedAtMs = Math.max(target.lastSelectorUpdatedAtMs ?? 0, selectorUpdatedAtMs);
    }
    this.persistTarget(target);
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
      activeTargetCount: targets.filter(target => target.lifecycle === 'ACTIVE').length,
      enrollingTargetCount: targets.filter(target => target.lifecycle === 'ENROLLING').length,
      missingGraceTargetCount: targets.filter(target => target.lifecycle === 'MISSING_GRACE').length,
      tombstoneCount: Object.keys(this.state.tombstones).length,
      deferredAdmissionCount: Object.keys(this.state.deferredAdmissions).length,
      durableStateCardinality: targets.length + Object.keys(this.state.tombstones).length
        + Object.keys(this.state.deferredAdmissions).length,
      serializedStateBytes: existsSync(this.path) ? statSync(this.path).size
        + (existsSync(this.journalPath) ? statSync(this.journalPath).size : 0) : 0,
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
  lifecycle: RetirementLifecycleState,
): RetirementOpenDisposition[] {
  const retiredAtMs = target.retiredAtMs ?? Number.NEGATIVE_INFINITY;
  const dispositions: RetirementOpenDisposition[] = [];
  for (const row of openRows) {
    if (row?.isOpen !== true || row?.verifiedTrade !== true) continue;
    const sourceBaseId = String(row?.baseId ?? row?.id ?? '').trim();
    const createdAtMs = directSourceTimeMs(row?.createdAt);
    if (!sourceBaseId || createdAtMs == null) {
      const rowId = String(row?.id ?? row?.baseId ?? 'unknown').trim() || 'unknown';
      dispositions.push({
        kind: 'invalid_lifecycle_open',
        evidenceKey: `retirement-invalid-open:${target.portfolioId}:${rowId}:${String(row?.createdAt)}`,
        sourceBaseId: sourceBaseId || null,
        createdAt: row?.createdAt,
      });
      continue;
    }

    const openSignal = signalFromInvestment(
      row, target, 'open', createdAtMs, 'investment.createdAt', observedAtMs,
    );
    // A malformed verified row is observable but can never become exposure.
    if (!openSignal) {
      dispositions.push({
        kind: 'invalid_lifecycle_open',
        evidenceKey: `retirement-invalid-open:${target.portfolioId}:${sourceBaseId}:${String(row?.createdAt)}`,
        sourceBaseId,
        createdAt: row?.createdAt,
      });
      continue;
    }
    if (createdAtMs < target.baselineAtMs) {
      if (!signalWasSeen(openSignal, lifecycle.hasSeen)) {
        dispositions.push({ kind: 'pre_selection_open_ignored', signal: openSignal });
      }
      continue;
    }
    if (createdAtMs > retiredAtMs) {
      if (!signalWasSeen(openSignal, lifecycle.hasSeen)) {
        dispositions.push({ kind: 'post_demotion_open_ignored', signal: openSignal });
      }
      continue;
    }
    // A handled close dominates every delayed representation of its OPEN lifecycle.
    if (lifecycle.hasHandledClose(sourceBaseId)) continue;

    if (lifecycle.isManagedSource(sourceBaseId)) {
      const updatedAtMs = directSourceTimeMs(row?.updatedAt);
      if (updatedAtMs == null || updatedAtMs <= target.processedThroughMs
        || updatedAtMs > retiredAtMs || row?.changes?.simIncrease !== true) continue;
      const currentSize = positive(row?.entrySize);
      const priorSize = positive(row?.changes?.entrySize);
      if (currentSize == null || priorSize == null || currentSize <= priorSize) continue;
      const increase = signalFromInvestment(
        row, target, 'increase', updatedAtMs, 'investment.updatedAt', observedAtMs,
        currentSize - priorSize, `size-${currentSize}`,
      );
      if (increase && !signalWasSeen(increase, lifecycle.hasSeen)) {
        dispositions.push({ kind: 'execute', signal: increase });
      }
      continue;
    }
    if (lifecycle.hasObservedOpen(sourceBaseId)) {
      const updatedAtMs = directSourceTimeMs(row?.updatedAt);
      const currentSize = positive(row?.entrySize);
      const priorSize = positive(row?.changes?.entrySize);
      if (updatedAtMs != null && updatedAtMs > target.processedThroughMs
        && updatedAtMs <= retiredAtMs && row?.changes?.simIncrease === true
        && currentSize != null && priorSize != null && currentSize > priorSize) {
        const increase = signalFromInvestment(
          row, target, 'increase', updatedAtMs, 'investment.updatedAt', observedAtMs,
          currentSize - priorSize, `size-${currentSize}`,
        );
        if (increase && !signalWasSeen(increase, lifecycle.hasSeen)) {
          dispositions.push({ kind: 'unowned_increase_ignored', signal: increase });
        }
      }
      continue;
    }
    if (signalWasSeen(openSignal, lifecycle.hasSeen)) continue;
    dispositions.push({ kind: 'execute', signal: openSignal });
  }
  return dispositions;
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

export interface ClosedHydrationClassification {
  signals: InvoSignal[];
  freshRowCount: number;
  unemittableFreshRows: Array<{ sourceBaseId: string | null; sourceTimeMs: number; reason: string }>;
}

/**
 * A CLOSED watermark may advance only across fresh rows that can be converted
 * into a close signal. Otherwise partially populated source rows (for example
 * missing closingPrice) would be consumed permanently and never retried.
 */
export function classifyClosedHydrationRows(
  closedRows: any[],
  target: EliteDirectTarget,
  processedThroughMs: number,
  boundaryIds: readonly string[],
  observedAtMs: number,
): ClosedHydrationClassification {
  const signals = closedSignalsAfterBoundary(
    closedRows, target, processedThroughMs, boundaryIds, observedAtMs,
  );
  const emitted = new Set(signals.map(signal => String(signal.sourceBaseId) + ':' + String(signal.sourceTimeMs ?? '')));
  const boundary = new Set(boundaryIds);
  const unemittableFreshRows: ClosedHydrationClassification['unemittableFreshRows'] = [];
  let freshRowCount = 0;

  for (const row of closedRows) {
    const sourceTimeMs = directSourceTimeMs(row?.closedAt) ?? directSourceTimeMs(row?.updatedAt);
    if (sourceTimeMs == null) continue; // ordering validation fails closed earlier.
    const sourceBaseId = String(row?.baseId ?? row?.id ?? '').trim();
    const isFresh = sourceTimeMs > processedThroughMs
      || (sourceTimeMs === processedThroughMs && (!sourceBaseId || !boundary.has(sourceBaseId)));
    if (!isFresh) continue;
    freshRowCount += 1;
    if (sourceBaseId && emitted.has(sourceBaseId + ':' + String(sourceTimeMs))) continue;

    let reason = 'signal_conversion_failed';
    if (!sourceBaseId) reason = 'missing_source_identity';
    else if (row?.isOpen !== false) reason = 'closed_row_not_closed';
    else if (row?.verifiedTrade !== true) reason = 'closed_row_unverified';
    else if (positive(row?.closingPrice) == null) reason = 'closing_price_unavailable';
    unemittableFreshRows.push({ sourceBaseId: sourceBaseId || null, sourceTimeMs, reason });
  }
  return { signals, freshRowCount, unemittableFreshRows };
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
