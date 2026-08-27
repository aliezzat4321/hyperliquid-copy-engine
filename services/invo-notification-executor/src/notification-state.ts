import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs';
import { dirname } from 'path';

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
  notionalUsd?: number;
  marginUsd?: number;
  leverage?: number;
  size?: number;
  sourceSize?: number;
  addCount?: number;
  estimatedOpenCostUsd?: number;
}

interface DiskState {
  seen: string[];
  /** Keyed by the Invo source position/base id, not by coin. */
  managed: Record<string, ManagedPosition>;
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
  private state: DiskState = { seen: [], managed: {} };
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

  snapshot() { return JSON.parse(JSON.stringify(this.state)) as DiskState; }
}
