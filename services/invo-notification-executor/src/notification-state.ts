import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs';
import { dirname } from 'path';
import type { InvoSignal } from './notification-signal.js';

export interface ExposureCheckpoint {
  atMs: number;
  size: number;
}

export interface FundingOracleCheckpoint {
  fundingTimeMs: number;
  observedAtMs: number;
  oraclePx: number;
}

export interface ManagedPosition {
  coin: string;
  sourceBaseId: string;
  sourceBaseShortId: string;
  sourcePostId: string;
  username?: string;
  ownerId?: string;
  portfolioId?: string;
  side: 'long' | 'short';
  openedAtMs: number;
  localBaseShortId?: string;
  paper?: boolean;
  entryMid?: number;
  entryPrice?: number;
  entryBookMid?: number;
  entryBookTimeMs?: number;
  entryBookReceivedAtMs?: number;
  entryBookAgeMs?: number;
  entrySpreadBps?: number;
  entrySlippageBps?: number;
  entrySlippageUsd?: number;
  entryFeeUsd?: number;
  entryNotionalExecutedUsd?: number;
  notionalUsd?: number;
  marginUsd?: number;
  leverage?: number;
  size?: number;
  sourceSize?: number;
  addCount?: number;
  estimatedOpenCostUsd?: number;
  unfilledOpenSize?: number;
  unresolvedAfterSourceClose?: boolean;
  sourceCloseRetryAttempts?: number;
  sourceCloseNextRetryAtMs?: number;
  sourceCloseLastReason?: string;
  pendingSourceClose?: InvoSignal;
  exposureCheckpoints?: ExposureCheckpoint[];
  fundingOracleCheckpoints?: FundingOracleCheckpoint[];
  fundingCarryUsd?: number;
  fundingAccruedThroughMs?: number;
  fundingIncompleteReason?: string;
  executionEvidenceVersion?: string;
  costModelVersion?: string;
}

const NOTIFICATION_STATE_VERSION = 1;

interface DiskState {
  version: typeof NOTIFICATION_STATE_VERSION;
  seen: string[];
  /** Keyed by the Invo source position/base id, not by coin. */
  managed: Record<string, ManagedPosition>;
  feedCursors: Record<string, FeedCursor>;
  feedBaselines: Record<string, number>;
  observedOpenSourceIds: string[];
  handledCloseSourceIds: string[];
}

export interface FeedCursor {
  postId: string;
  observedAtMs: number;
  source: string;
}

export function synthesizeLegacyPendingSourceClose(position: ManagedPosition): InvoSignal | null {
  if (!position.unresolvedAfterSourceClose || position.pendingSourceClose) return position.pendingSourceClose ?? null;
  const size = Number(position.size);
  if (!position.sourceBaseId || !position.coin || !(size > 0)) return null;
  const leverage = Number(position.leverage);
  const sourcePostId = position.sourcePostId || `legacy-${position.sourceBaseId}`;
  return {
    key: `${sourcePostId}:close:${position.sourceBaseId}:legacy-reconcile`,
    postId: sourcePostId,
    action: 'close',
    // Persisted provenance only: this migration must be identical on every restart.
    observedAtMs: position.openedAtMs,
    sourceTimeMs: null,
    sourceTimeField: 'legacy_unresolved_source_close',
    ownerId: '',
    username: position.username ?? '',
    portfolioId: '',
    sourceBaseId: position.sourceBaseId,
    sourceBaseShortId: position.sourceBaseShortId ?? '',
    coin: position.coin,
    side: position.side,
    leverage: Number.isFinite(leverage) && leverage > 0 ? Math.max(1, Math.trunc(leverage)) : 1,
    entryPrice: Number.isFinite(Number(position.entryPrice)) ? Number(position.entryPrice) : null,
    closingPrice: null,
    entrySize: Number.isFinite(Number(position.sourceSize)) ? Number(position.sourceSize) : null,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function requireRecord(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) throw new Error(`${path} must be an object`);
  return value;
}

function requireString(value: unknown, path: string, allowEmpty = false): asserts value is string {
  if (typeof value !== 'string' || (!allowEmpty && value.length === 0)) {
    throw new Error(`${path} must be ${allowEmpty ? 'a string' : 'a non-empty string'}`);
  }
}

function requireFiniteNumber(value: unknown, path: string): asserts value is number {
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`${path} must be a finite number`);
}

function requireOptionalString(record: Record<string, unknown>, key: string, path: string) {
  if (record[key] !== undefined) requireString(record[key], `${path}.${key}`, true);
}

function requireOptionalNumber(record: Record<string, unknown>, key: string, path: string) {
  if (record[key] !== undefined) requireFiniteNumber(record[key], `${path}.${key}`);
}

function requireOptionalBoolean(record: Record<string, unknown>, key: string, path: string) {
  if (record[key] !== undefined && typeof record[key] !== 'boolean') throw new Error(`${path}.${key} must be a boolean`);
}

function validateStringArray(value: unknown, path: string): string[] {
  if (!Array.isArray(value)) throw new Error(`${path} must be an array`);
  value.forEach((item, index) => requireString(item, `${path}[${index}]`));
  return value as string[];
}

function validatePendingSourceClose(value: unknown, path: string): asserts value is InvoSignal {
  const signal = requireRecord(value, path);
  for (const key of ['key', 'postId', 'ownerId', 'username', 'portfolioId', 'sourceBaseId', 'sourceBaseShortId', 'coin'] as const) {
    requireString(signal[key], `${path}.${key}`, ['ownerId', 'username', 'portfolioId', 'sourceBaseShortId'].includes(key));
  }
  if (!['open', 'increase', 'close'].includes(String(signal.action))) throw new Error(`${path}.action is invalid`);
  if (!['long', 'short'].includes(String(signal.side))) throw new Error(`${path}.side is invalid`);
  requireFiniteNumber(signal.observedAtMs, `${path}.observedAtMs`);
  requireFiniteNumber(signal.leverage, `${path}.leverage`);
  for (const key of ['sourceTimeMs', 'openedSourceTimeMs', 'entryPrice', 'closingPrice', 'entrySize', 'resultingSourceSize'] as const) {
    if (signal[key] !== undefined && signal[key] !== null) requireFiniteNumber(signal[key], `${path}.${key}`);
  }
  if (signal.sourceTimeField !== null) requireString(signal.sourceTimeField, `${path}.sourceTimeField`, true);
}

export function validateManagedPosition(value: unknown, path = 'managed position'): ManagedPosition {
  const position = requireRecord(value, path);
  for (const key of ['coin', 'sourceBaseId', 'sourcePostId'] as const) {
    requireString(position[key], `${path}.${key}`);
  }
  requireString(position.sourceBaseShortId, `${path}.sourceBaseShortId`, true);
  if (position.side !== 'long' && position.side !== 'short') throw new Error(`${path}.side is invalid`);
  requireFiniteNumber(position.openedAtMs, `${path}.openedAtMs`);
  if (position.openedAtMs <= 0) throw new Error(`${path}.openedAtMs must be positive`);
  for (const key of ['username', 'ownerId', 'portfolioId', 'localBaseShortId', 'sourceCloseLastReason',
    'fundingIncompleteReason', 'executionEvidenceVersion', 'costModelVersion'] as const) requireOptionalString(position, key, path);
  for (const key of ['entryMid', 'entryPrice', 'entryBookMid', 'entryBookTimeMs', 'entryBookReceivedAtMs',
    'entryBookAgeMs', 'entrySpreadBps', 'entrySlippageBps', 'entrySlippageUsd', 'entryFeeUsd',
    'entryNotionalExecutedUsd', 'notionalUsd', 'marginUsd', 'leverage', 'size', 'sourceSize', 'addCount',
    'estimatedOpenCostUsd', 'unfilledOpenSize', 'sourceCloseRetryAttempts', 'sourceCloseNextRetryAtMs',
    'fundingCarryUsd', 'fundingAccruedThroughMs'] as const) requireOptionalNumber(position, key, path);
  for (const key of ['paper', 'unresolvedAfterSourceClose'] as const) requireOptionalBoolean(position, key, path);
  if (position.pendingSourceClose !== undefined) validatePendingSourceClose(position.pendingSourceClose, `${path}.pendingSourceClose`);
  if (position.exposureCheckpoints !== undefined) {
    if (!Array.isArray(position.exposureCheckpoints)) throw new Error(`${path}.exposureCheckpoints must be an array`);
    position.exposureCheckpoints.forEach((checkpoint, index) => {
      const item = requireRecord(checkpoint, `${path}.exposureCheckpoints[${index}]`);
      requireFiniteNumber(item.atMs, `${path}.exposureCheckpoints[${index}].atMs`);
      requireFiniteNumber(item.size, `${path}.exposureCheckpoints[${index}].size`);
      if (item.atMs <= 0) throw new Error(`${path}.exposureCheckpoints[${index}].atMs must be positive`);
    });
  }
  if (position.fundingOracleCheckpoints !== undefined) {
    if (!Array.isArray(position.fundingOracleCheckpoints)) throw new Error(`${path}.fundingOracleCheckpoints must be an array`);
    position.fundingOracleCheckpoints.forEach((checkpoint, index) => {
      const item = requireRecord(checkpoint, `${path}.fundingOracleCheckpoints[${index}]`);
      requireFiniteNumber(item.fundingTimeMs, `${path}.fundingOracleCheckpoints[${index}].fundingTimeMs`);
      requireFiniteNumber(item.observedAtMs, `${path}.fundingOracleCheckpoints[${index}].observedAtMs`);
      requireFiniteNumber(item.oraclePx, `${path}.fundingOracleCheckpoints[${index}].oraclePx`);
      if (item.fundingTimeMs <= 0 || item.observedAtMs <= 0) throw new Error(`${path}.fundingOracleCheckpoints[${index}] timestamps must be positive`);
    });
  }
  return position as unknown as ManagedPosition;
}

function normalizeManaged(raw: unknown): Record<string, ManagedPosition> {
  const input = requireRecord(raw, 'state.managed');
  const normalized: Record<string, ManagedPosition> = {};
  for (const [legacyKey, rawPosition] of Object.entries(input)) {
    const position = validateManagedPosition(rawPosition, `state.managed.${legacyKey}`);
    // v1 keyed by coin. v2 keys by sourceBaseId so many traders may hold BTC simultaneously.
    const pendingSourceClose = synthesizeLegacyPendingSourceClose(position);
    if (normalized[position.sourceBaseId]) throw new Error(`state.managed has duplicate sourceBaseId ${position.sourceBaseId}`);
    normalized[position.sourceBaseId] = pendingSourceClose && !position.pendingSourceClose
      ? { ...position, pendingSourceClose }
      : position;
  }
  return normalized;
}

function validateFeedCursors(value: unknown): Record<string, FeedCursor> {
  const raw = requireRecord(value, 'state.feedCursors');
  const cursors: Record<string, FeedCursor> = {};
  for (const [feed, rawCursor] of Object.entries(raw)) {
    requireString(feed, 'state.feedCursors key');
    const cursor = requireRecord(rawCursor, `state.feedCursors.${feed}`);
    requireString(cursor.postId, `state.feedCursors.${feed}.postId`);
    requireFiniteNumber(cursor.observedAtMs, `state.feedCursors.${feed}.observedAtMs`);
    requireString(cursor.source, `state.feedCursors.${feed}.source`);
    cursors[feed] = cursor as unknown as FeedCursor;
  }
  return cursors;
}

function validateFeedBaselines(value: unknown): Record<string, number> {
  const raw = requireRecord(value, 'state.feedBaselines');
  for (const [feed, baseline] of Object.entries(raw)) {
    requireString(feed, 'state.feedBaselines key');
    requireFiniteNumber(baseline, `state.feedBaselines.${feed}`);
  }
  return raw as Record<string, number>;
}

function parseDiskState(value: unknown, maxSeen: number): DiskState {
  const parsed = requireRecord(value, 'state');
  const isLegacy = parsed.version === undefined;
  if (!isLegacy && parsed.version !== NOTIFICATION_STATE_VERSION) {
    throw new Error(`unsupported notification state version: ${String(parsed.version)}`);
  }
  if (isLegacy && (!Object.hasOwn(parsed, 'seen') || !Object.hasOwn(parsed, 'managed'))) {
    throw new Error('unversioned state does not match the known legacy shape');
  }
  const required = <T>(key: string, legacyDefault: T): unknown => {
    if (Object.hasOwn(parsed, key)) return parsed[key];
    if (isLegacy) return legacyDefault;
    throw new Error(`state.${key} is required`);
  };
  const seen = validateStringArray(required('seen', []), 'state.seen');
  const managed = normalizeManaged(required('managed', {}));
  const feedCursors = validateFeedCursors(required('feedCursors', {}));
  const explicitBaselines = validateFeedBaselines(required('feedBaselines', {}));
  const observedOpenSourceIds = validateStringArray(required('observedOpenSourceIds', []), 'state.observedOpenSourceIds');
  const explicitHandled = validateStringArray(required('handledCloseSourceIds', []), 'state.handledCloseSourceIds');
  return {
    version: NOTIFICATION_STATE_VERSION,
    seen,
    managed,
    feedCursors,
    feedBaselines: {
      ...Object.fromEntries(Object.entries(feedCursors).map(([feed, cursor]) => [feed, cursor.observedAtMs])),
      ...explicitBaselines,
    },
    observedOpenSourceIds,
    handledCloseSourceIds: [...new Set([
      ...explicitHandled,
      ...seen.flatMap(key => key.startsWith('source-close:') && key.length > 'source-close:'.length
        ? [key.slice('source-close:'.length)] : []),
    ])].slice(-maxSeen),
  };
}

export class NotificationState {
  private state: DiskState = { version: NOTIFICATION_STATE_VERSION, seen: [], managed: {}, feedCursors: {}, feedBaselines: {}, observedOpenSourceIds: [], handledCloseSourceIds: [] };
  private seen = new Set<string>();
  private observedOpenSourceIds = new Set<string>();
  private handledCloseSourceIds = new Set<string>();

  constructor(private readonly path: string, private readonly maxSeen = 20_000) {
    this.load();
  }

  private load() {
    if (!existsSync(this.path)) return;
    try {
      this.state = parseDiskState(JSON.parse(readFileSync(this.path, 'utf8')), this.maxSeen);
      this.seen = new Set(this.state.seen);
      this.observedOpenSourceIds = new Set(this.state.observedOpenSourceIds);
      this.handledCloseSourceIds = new Set(this.state.handledCloseSourceIds);
    } catch (err) {
      console.error(JSON.stringify({ type: 'state_load_error', path: this.path, error: String(err) }));
      throw err;
    }
  }

  private save() {
    mkdirSync(dirname(this.path), { recursive: true });
    const temp = `${this.path}.tmp`;
    writeFileSync(temp, JSON.stringify(this.state, null, 2));
    renameSync(temp, this.path);
  }

  hasSeen(key: string) { return this.seen.has(key); }

  markSeen(key: string) {
    if (this.seen.has(key)) return;
    this.state.seen.push(key);
    this.seen.add(key);
    while (this.state.seen.length > this.maxSeen) {
      const old = this.state.seen.shift();
      if (old) this.seen.delete(old);
    }
    this.save();
  }

  hasObservedOpen(sourceBaseId: string) { return this.observedOpenSourceIds.has(sourceBaseId); }

  markObservedOpen(sourceBaseId: string) {
    if (!sourceBaseId || this.observedOpenSourceIds.has(sourceBaseId)) return;
    this.state.observedOpenSourceIds.push(sourceBaseId);
    this.observedOpenSourceIds.add(sourceBaseId);
    while (this.state.observedOpenSourceIds.length > this.maxSeen) {
      const old = this.state.observedOpenSourceIds.shift();
      if (old) this.observedOpenSourceIds.delete(old);
    }
    this.save();
  }

  hasHandledClose(sourceBaseId: string) { return this.handledCloseSourceIds.has(sourceBaseId); }

  markHandledClose(sourceBaseId: string) {
    if (!sourceBaseId || this.handledCloseSourceIds.has(sourceBaseId)) return;
    this.state.handledCloseSourceIds.push(sourceBaseId);
    this.handledCloseSourceIds.add(sourceBaseId);
    while (this.state.handledCloseSourceIds.length > this.maxSeen) {
      const old = this.state.handledCloseSourceIds.shift();
      if (old) this.handledCloseSourceIds.delete(old);
    }
    this.save();
  }

  getManagedBySource(sourceBaseId: string) {
    return this.state.managed[sourceBaseId] ?? null;
  }

  getManagedForCoin(coin: string) {
    const wanted = coin.toUpperCase();
    return Object.values(this.state.managed).filter(position => position.coin.toUpperCase() === wanted);
  }

  setManaged(position: ManagedPosition) {
    this.state.managed[position.sourceBaseId] = position;
    this.save();
  }

  clearManagedBySource(sourceBaseId: string) {
    delete this.state.managed[sourceBaseId];
    this.save();
  }

  managedCount() {
    return Object.keys(this.state.managed).length;
  }

  getFeedCursor(feed: string): FeedCursor | null {
    return this.state.feedCursors[feed] ?? null;
  }

  setFeedCursor(feed: string, cursor: FeedCursor) {
    this.state.feedCursors[feed] = cursor;
    this.state.feedBaselines[feed] = cursor.observedAtMs;
    this.save();
  }

  hasFeedBaseline(feed: string) {
    return Number.isFinite(this.state.feedBaselines[feed]);
  }

  markFeedBaselined(feed: string, observedAtMs: number) {
    this.state.feedBaselines[feed] = observedAtMs;
    this.save();
  }

  snapshot() { return JSON.parse(JSON.stringify(this.state)) as DiskState; }
}
