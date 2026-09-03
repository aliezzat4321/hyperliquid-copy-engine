import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs';
import { dirname } from 'path';
import { InvoSignal } from './notification-signal.js';

export type TraderLifecycle = 'active' | 'stale' | 'inactive';

export interface EvidencePolicy {
  minEvents: number;
  minObservationDays: number;
  staleAfterMs: number;
  inactiveAfterMs: number;
}

export interface TrackedTrader {
  id: string;
  ownerId: string | null;
  usernames: string[];
  portfolioIds: string[];
  wallet: string | null;
  sources: string[];
  firstDiscoveredAtMs: number;
  lastDiscoveredAtMs: number;
  firstEventAtMs: number | null;
  lastEventAtMs: number | null;
  eventCount: number;
  symbols: string[];
  observationDays: string[];
  assessmentEligibleAtMs: number | null;
  missingOrFailedReasons: Record<string, number>;
}

interface TrackerDiskState {
  version: 1;
  policy: EvidencePolicy;
  traders: Record<string, TrackedTrader>;
  eventKeys: string[];
}

export interface TraderView extends TrackedTrader {
  lifecycle: TraderLifecycle;
  trackable: boolean;
  receivingNotifications: boolean;
  enoughEvents: boolean;
  shadowAssessable: boolean;
  freshnessMs: number;
}

function strings(...values: unknown[]): string[] {
  return [...new Set(values.filter(v => typeof v === 'string' && v.trim()).map(v => String(v).trim()))].sort();
}

function username(post: any): string | null {
  const value = post?.update?.owner?.username ?? post?.owner?.username;
  return typeof value === 'string' && value.trim() ? value.trim().replace(/^@/, '').toLowerCase() : null;
}

function identity(post: any): { id: string; ownerId: string | null; username: string | null; portfolioId: string | null } | null {
  const ownerIdValue = post?.update?.owner?.id ?? post?.owner?.id;
  const ownerId = ownerIdValue == null || String(ownerIdValue).trim() === '' ? null : String(ownerIdValue);
  const name = username(post);
  const portfolioValue = post?.update?.portfolio?.id ?? post?.portfolio?.id;
  const portfolioId = portfolioValue == null || String(portfolioValue).trim() === '' ? null : String(portfolioValue);
  const id = ownerId ? `invo-user:${ownerId}` : portfolioId ? `invo-portfolio:${portfolioId}` : name ? `invo-username:${name}` : '';
  return id ? { id, ownerId, username: name, portfolioId } : null;
}

/** A stable reason for why a discovered post cannot become a canonical Lane 3 event. */
export function untrackablePostReason(post: any): string | null {
  const update = post?.update;
  if (!update) return 'missing_update';
  if (!update?.ticker) return 'missing_ticker';
  if (update.verifiedTrade !== true) return 'not_verified_trade';
  if (typeof update.directionLong !== 'boolean') return 'missing_direction';
  if (!(Number(update.leverage) > 0)) return 'missing_or_invalid_leverage';
  if (!(post?.id ?? update?.id)) return 'missing_post_id';
  if (!(update?.baseId ?? update?.id)) return 'missing_base_id';
  if (!(update?.portfolio?.id)) return 'missing_portfolio_id';
  if (!(update?.owner?.id ?? post?.owner?.id)) return 'missing_owner_id';
  return null;
}

function utcDay(ms: number): string { return new Date(ms).toISOString().slice(0, 10); }

export class TraderTracker {
  private state: TrackerDiskState;
  private eventKeys: Set<string>;

  constructor(private readonly path: string, policy: EvidencePolicy, private readonly maxEventKeys = 100_000) {
    this.state = { version: 1, policy, traders: {}, eventKeys: [] };
    if (existsSync(path)) {
      try {
        const parsed = JSON.parse(readFileSync(path, 'utf8')) as Partial<TrackerDiskState>;
        this.state = { version: 1, policy, traders: parsed.traders ?? {}, eventKeys: parsed.eventKeys ?? [] };
      } catch (err) {
        console.error(JSON.stringify({ type: 'trader_tracker_load_error', path, error: String(err) }));
      }
    }
    // Runtime configuration is the predeclared policy for all future assessments.
    this.state.policy = policy;
    this.eventKeys = new Set(this.state.eventKeys);
  }

  private save() {
    mkdirSync(dirname(this.path), { recursive: true });
    const temp = `${this.path}.tmp`;
    writeFileSync(temp, JSON.stringify(this.state, null, 2));
    renameSync(temp, this.path);
  }

  observe(post: any, source: string, signal: InvoSignal | null, observedAtMs = Date.now(), persist = true): string | null {
    const found = identity(post);
    if (!found) return null;
    let trader = this.state.traders[found.id];
    if (!trader && found.ownerId) {
      const alias = Object.values(this.state.traders).find(candidate =>
        candidate.ownerId === found.ownerId ||
        (found.portfolioId != null && candidate.portfolioIds.includes(found.portfolioId)) ||
        (found.username != null && candidate.usernames.includes(found.username)),
      );
      if (alias) {
        delete this.state.traders[alias.id];
        alias.id = found.id;
        alias.ownerId = found.ownerId;
        this.state.traders[found.id] = alias;
        trader = alias;
      }
    }
    if (!trader) {
      trader = this.state.traders[found.id] = {
        id: found.id, ownerId: found.ownerId, usernames: [], portfolioIds: [], wallet: null,
        sources: [], firstDiscoveredAtMs: observedAtMs, lastDiscoveredAtMs: observedAtMs,
        firstEventAtMs: null, lastEventAtMs: null, eventCount: 0, symbols: [], observationDays: [],
        assessmentEligibleAtMs: null,
        missingOrFailedReasons: {},
      };
    }
    trader.ownerId ||= found.ownerId;
    trader.usernames = strings(...trader.usernames, found.username);
    trader.portfolioIds = strings(...trader.portfolioIds, found.portfolioId, signal?.portfolioId);
    trader.sources = strings(...trader.sources, source);

    const reason = signal ? null : untrackablePostReason(post);
    const rejectedKey = `rejected:${String(post?.id ?? post?.update?.id ?? '')}:${reason ?? 'unknown'}`;
    const newEvent = Boolean(signal && !this.eventKeys.has(signal.key));
    const newRejection = Boolean(reason && !this.eventKeys.has(rejectedKey));
    if (newEvent || newRejection) trader.lastDiscoveredAtMs = Math.max(trader.lastDiscoveredAtMs, observedAtMs);
    if (reason && newRejection) {
      trader.missingOrFailedReasons[reason] = (trader.missingOrFailedReasons[reason] ?? 0) + 1;
      this.eventKeys.add(rejectedKey);
      this.state.eventKeys.push(rejectedKey);
    }
    if (signal && newEvent) {
      this.eventKeys.add(signal.key);
      this.state.eventKeys.push(signal.key);
      while (this.state.eventKeys.length > this.maxEventKeys) {
        const removed = this.state.eventKeys.shift();
        if (removed) this.eventKeys.delete(removed);
      }
      const eventAt = signal.sourceTimeMs ?? observedAtMs;
      trader.eventCount += 1;
      trader.firstEventAtMs = trader.firstEventAtMs == null ? eventAt : Math.min(trader.firstEventAtMs, eventAt);
      trader.lastEventAtMs = trader.lastEventAtMs == null ? eventAt : Math.max(trader.lastEventAtMs, eventAt);
      trader.symbols = strings(...trader.symbols, signal.coin);
      // Evidence accrues on collection days, not source-history dates. A startup page
      // containing old posts therefore cannot instantly satisfy a prospective window.
      trader.observationDays = strings(...trader.observationDays, utcDay(observedAtMs));
    }
    trader.assessmentEligibleAtMs ??= (
      trader.ownerId && trader.portfolioIds.length &&
      trader.eventCount >= this.state.policy.minEvents &&
      trader.observationDays.length >= this.state.policy.minObservationDays
    ) ? observedAtMs : null;
    if (persist) this.save();
    return found.id;
  }

  flush() { this.save(); }

  recordFailure(traderId: string | null, reason: string) {
    if (!traderId || !this.state.traders[traderId]) return;
    const reasons = this.state.traders[traderId].missingOrFailedReasons;
    reasons[reason] = (reasons[reason] ?? 0) + 1;
    this.save();
  }

  view(nowMs = Date.now()): TraderView[] {
    const p = this.state.policy;
    return Object.values(this.state.traders).map(trader => {
      const freshnessMs = Math.max(0, nowMs - trader.lastDiscoveredAtMs);
      const lifecycle: TraderLifecycle = freshnessMs >= p.inactiveAfterMs ? 'inactive' : freshnessMs >= p.staleAfterMs ? 'stale' : 'active';
      const trackable = Boolean(trader.ownerId && trader.portfolioIds.length);
      const receivingNotifications = trader.eventCount > 0;
      const enoughEvents = trader.eventCount >= p.minEvents && trader.observationDays.length >= p.minObservationDays;
      return { ...trader, lifecycle, trackable, receivingNotifications, enoughEvents, shadowAssessable: lifecycle === 'active' && trackable && enoughEvents && trader.assessmentEligibleAtMs != null, freshnessMs };
    }).sort((a, b) => a.id.localeCompare(b.id));
  }

  isShadowAssessable(traderId: string | null, nowMs = Date.now()): boolean {
    return this.view(nowMs).some(t => t.id === traderId && t.shadowAssessable);
  }

  report(nowMs = Date.now()) {
    const traders = this.view(nowMs);
    const count = (predicate: (t: TraderView) => boolean) => traders.filter(predicate).length;
    return {
      policy: this.state.policy,
      funnel: {
        discovered: traders.length,
        trackable: count(t => t.trackable),
        activeTracked: count(t => t.trackable && t.lifecycle === 'active'),
        receivingNotifications: count(t => t.receivingNotifications),
        enoughEvents: count(t => t.enoughEvents),
        shadowAssessable: count(t => t.shadowAssessable),
        stale: count(t => t.lifecycle === 'stale'),
        inactive: count(t => t.lifecycle === 'inactive'),
      },
      assessmentQueue: traders.filter(t => t.shadowAssessable).map(t => t.id),
      traders,
    };
  }
}
