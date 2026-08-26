import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs';
import { dirname } from 'path';

export interface ManagedPosition {
  coin: string;
  sourceBaseId: string;
  sourceBaseShortId: string;
  sourcePostId: string;
  side: 'long' | 'short';
  openedAtMs: number;
  localBaseShortId?: string;
}

interface DiskState {
  seen: string[];
  managed: Record<string, ManagedPosition>;
}

export class NotificationState {
  private state: DiskState = { seen: [], managed: {} };
  private seen = new Set<string>();

  constructor(private readonly path: string, private readonly maxSeen = 4000) {
    this.load();
  }

  private load() {
    if (!existsSync(this.path)) return;
    try {
      const parsed = JSON.parse(readFileSync(this.path, 'utf8')) as DiskState;
      this.state = { seen: parsed.seen ?? [], managed: parsed.managed ?? {} };
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

  getManaged(coin: string) { return this.state.managed[coin.toUpperCase()] ?? null; }

  setManaged(position: ManagedPosition) {
    this.state.managed[position.coin.toUpperCase()] = position;
    this.save();
  }

  clearManaged(coin: string) {
    delete this.state.managed[coin.toUpperCase()];
    this.save();
  }

  snapshot() { return JSON.parse(JSON.stringify(this.state)) as DiskState; }
}
