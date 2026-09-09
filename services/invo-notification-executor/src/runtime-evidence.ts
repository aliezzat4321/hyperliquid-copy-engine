import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs';
import { dirname } from 'path';

export interface RuntimeEvidenceOptions {
  latestPath: string;
  historyPath: string;
  historyIntervalMs: number;
  live: boolean;
  discoverySurfaces: string[];
  evidencePolicy: unknown;
}

interface RuntimeCounters {
  auditEvents: number;
  eligibleSignals: number;
  causalDecisions: number;
  explicitRejectsOrGaps: number;
  notificationWakes: number;
  successfulPolls: number;
  failedPolls: number;
  eventTypes: Record<string, number>;
  rejectReasons: Record<string, number>;
  surfaceSuccesses: Record<string, number>;
  surfaceFailures: Record<string, number>;
  lastSurfaceSuccessAt: Record<string, string>;
}

interface RuntimeEvidenceState {
  schema: 'lane3-runtime-acceptance/v1';
  windowStartedAt: string;
  lastUpdatedAt: string;
  lastSuccessfulPollAt: string | null;
  lastHistoryAt: string | null;
  realTradingEnabled: false;
  collectionMode: 'prospective-shadow';
  collectionIndependentOfAcceptance: true;
  discoverySurfaces: string[];
  counters: RuntimeCounters;
  baselineFunnel: Record<string, number> | null;
  currentFunnel: Record<string, number> | null;
  funnelDelta: Record<string, number> | null;
  assessmentQueue: string[];
  traders: unknown[];
  unresolvedExposureCount: number;
}

const CAUSAL_TYPES = new Set([
  'skip', 'execution_error', 'shadow_opened', 'shadow_opened_from_increase',
  'shadow_reupped', 'shadow_closed', 'shadow_close_unpriced', 'opened',
  'opened_from_increase', 'reupped', 'closed', 'close_already_flat',
]);
const REJECT_TYPES = new Set(['skip', 'execution_error', 'shadow_close_unpriced']);

function blankCounters(): RuntimeCounters {
  return {
    auditEvents: 0, eligibleSignals: 0, causalDecisions: 0,
    explicitRejectsOrGaps: 0, notificationWakes: 0, successfulPolls: 0,
    failedPolls: 0, eventTypes: {}, rejectReasons: {}, surfaceSuccesses: {},
    surfaceFailures: {}, lastSurfaceSuccessAt: {},
  };
}

function increment(values: Record<string, number>, key: string) {
  values[key] = (values[key] ?? 0) + 1;
}

export class Lane3RuntimeEvidence {
  private state: RuntimeEvidenceState;

  constructor(private readonly options: RuntimeEvidenceOptions, nowMs = Date.now()) {
    const now = new Date(nowMs).toISOString();
    this.state = {
      schema: 'lane3-runtime-acceptance/v1', windowStartedAt: now, lastUpdatedAt: now,
      lastSuccessfulPollAt: null, lastHistoryAt: null, realTradingEnabled: false,
      collectionMode: 'prospective-shadow', collectionIndependentOfAcceptance: true,
      discoverySurfaces: [...new Set(options.discoverySurfaces)].sort(),
      evidencePolicy: null,
      counters: blankCounters(), baselineFunnel: null, currentFunnel: null,
      funnelDelta: null, assessmentQueue: [], traders: [], unresolvedExposureCount: 0,
    };
    if (existsSync(options.latestPath)) {
      try {
        const saved = JSON.parse(readFileSync(options.latestPath, 'utf8')) as RuntimeEvidenceState;
        if (saved.schema === this.state.schema && saved.realTradingEnabled === false) {
          this.state = { ...this.state, ...saved, discoverySurfaces: this.state.discoverySurfaces };
        }
      } catch {
        // The audit ledger remains canonical. A corrupt derived snapshot starts a new
        // fail-closed measurement window rather than stopping shadow collection.
      }
    }
  }

  recordAuditEvent(event: Record<string, unknown>) {
    if (this.options.live) return;
    const type = String(event.type ?? 'unknown');
    this.state.counters.auditEvents += 1;
    increment(this.state.counters.eventTypes, type);
    if (event.signal) this.state.counters.eligibleSignals += 1;
    if (CAUSAL_TYPES.has(type) && event.signal) this.state.counters.causalDecisions += 1;
    if (REJECT_TYPES.has(type) && event.signal) {
      this.state.counters.explicitRejectsOrGaps += 1;
      increment(this.state.counters.rejectReasons, String(event.reason ?? type));
    }
    if (String(event.wakeSource ?? '').startsWith('push_')) this.state.counters.notificationWakes += 1;
    this.state.lastUpdatedAt = new Date().toISOString();
    this.persistLatest();
  }

  recordPoll(surface: string, success: boolean, population: any, unresolvedExposureCount: number, nowMs = Date.now()) {
    if (this.options.live) return;
    const now = new Date(nowMs).toISOString();
    this.state.lastUpdatedAt = now;
    if (success) {
      this.state.counters.successfulPolls += 1;
      increment(this.state.counters.surfaceSuccesses, surface);
      this.state.counters.lastSurfaceSuccessAt[surface] = now;
      this.state.lastSuccessfulPollAt = now;
    } else {
      this.state.counters.failedPolls += 1;
      increment(this.state.counters.surfaceFailures, surface);
    }
    const funnel = population?.funnel && typeof population.funnel === 'object'
      ? { ...population.funnel } as Record<string, number> : null;
    this.state.baselineFunnel ??= funnel;
    this.state.currentFunnel = funnel;
    this.state.funnelDelta = funnel && this.state.baselineFunnel
      ? Object.fromEntries(Object.entries(funnel).map(([key, value]) => [key, value - (this.state.baselineFunnel?.[key] ?? 0)]))
      : null;
    this.state.assessmentQueue = Array.isArray(population?.assessmentQueue) ? population.assessmentQueue : [];
    this.state.traders = Array.isArray(population?.traders) ? population.traders : [];
    this.state.evidencePolicy = population?.policy ?? this.state.evidencePolicy;
    this.state.unresolvedExposureCount = unresolvedExposureCount;
    this.persist(nowMs);
  }

  snapshot(): RuntimeEvidenceState | null {
    return this.options.live ? null : JSON.parse(JSON.stringify(this.state));
  }

  private persist(nowMs: number) {
    mkdirSync(dirname(this.options.latestPath), { recursive: true });
    mkdirSync(dirname(this.options.historyPath), { recursive: true });
    this.persistLatest();
    const lastHistoryMs = this.state.lastHistoryAt ? Date.parse(this.state.lastHistoryAt) : 0;
    if (!lastHistoryMs || nowMs - lastHistoryMs >= this.options.historyIntervalMs) {
      this.state.lastHistoryAt = new Date(nowMs).toISOString();
      appendFileSync(this.options.historyPath, `${JSON.stringify(this.state)}\n`);
      this.persistLatest();
    }
  }

  private persistLatest() {
    mkdirSync(dirname(this.options.latestPath), { recursive: true });
    const temp = `${this.options.latestPath}.tmp`;
    writeFileSync(temp, `${JSON.stringify(this.state, null, 2)}\n`);
    renameSync(temp, this.options.latestPath);
  }
}
