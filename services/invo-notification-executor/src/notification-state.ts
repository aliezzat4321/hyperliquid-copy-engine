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

interface DiskState {
  seen: string[];
  /** Keyed by the Invo source position/base id, not by coin. */
  managed: Record<string, ManagedPosition>;
  feedCursors: Record<string, FeedCursor>;
}

export interface FeedCursor {
  postId: string;
  observedAtMs: number;
  source: string;
}

function normalizeManaged(raw: Record<string, ManagedPosition> | undefined): Record<string, ManagedPosition> {
  const normalized: Record<string, ManagedPosition> = {};
  for (const [legacyKey, position] of Object.entries(raw ?? {})) {
    if (!position || !position.sourceBaseId) continue;
    // v1 keyed by coin. v2 keys by sourceBaseId so many traders may hold BTC simultaneously.
    normalized[position.sourceBaseId || legacyKey] = position;
  }
  return normalized;
}

export class NotificationState {
  private state: DiskState = { seen: [], managed: {}, feedCursors: {} };
  private seen = new Set<string>();

  constructor(private readonly path: string, private readonly maxSeen = 20_000) {
    this.load();
  }

  private load() {
    if (!existsSync(this.path)) return;
    try {
      const parsed = JSON.parse(readFileSync(this.path, 'utf8')) as DiskState;
      this.state = {
        seen: parsed.seen ?? [],
        managed: normalizeManaged(parsed.managed),
        feedCursors: parsed.feedCursors ?? {},
      };
      this.seen = new Set(this.state.seen);
    } catch (err) {
      console.error(JSON.stringify({ type: 'state_load_error', path: this.path, error: String(err) }));
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
    this.save();
  }

  snapshot() { return JSON.parse(JSON.stringify(this.state)) as DiskState; }
}
