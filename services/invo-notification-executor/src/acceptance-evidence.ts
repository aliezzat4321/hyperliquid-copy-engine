import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'fs';
import { dirname } from 'path';
import type { EvidencePolicy, TraderView } from './trader-tracker.js';

type Funnel = Record<string, number>;

export interface PopulationReport {
  policy: EvidencePolicy;
  funnel: Funnel;
  assessmentQueue: string[];
  traders: TraderView[];
}

interface AuditSummary {
  rows: number;
  malformedRows: number;
  canonicalSignals: number;
  causalDecisions: number;
  shadowEvents: number;
  explicitRejectsOrGaps: number;
  eventTypes: Record<string, number>;
  rejectReasons: Record<string, number>;
}

interface EvidenceSnapshot {
  schemaVersion: 1;
  contractId: 'LANE3-RUNTIME-ACCEPTANCE-v1';
  observedAt: string;
  windowStartedAt: string;
  live: false;
  policy: EvidencePolicy;
  baseline: { funnel: Funnel; audit: AuditSummary };
  current: { funnel: Funnel; audit: AuditSummary; eventCount: number; observationDays: string[]; symbols: string[]; missingOrFailedReasons: Record<string, number>; traders: PopulationReport['traders'] };
  growth: { discovered: number; receivingNotifications: number; shadowAssessable: number; canonicalSignals: number; causalDecisions: number };
  checks: { realTradingDisabled: true; populationGrowing: boolean; freshCausalEvidence: boolean; everyCanonicalSignalAccountedFor: boolean };
  status: 'COLLECTING' | 'RUNTIME_ACTIVITY_PROVEN';
  promotionVerdict: 'NOT_EVALUATED';
}

const decisionTypes = new Set([
  'shadow_opened', 'shadow_opened_from_increase', 'shadow_reupped', 'shadow_closed',
  'skip', 'execution_error',
]);
const shadowTypes = new Set(['shadow_opened', 'shadow_opened_from_increase', 'shadow_reupped', 'shadow_closed']);

function increment(target: Record<string, number>, key: string) {
  target[key] = (target[key] ?? 0) + 1;
}

export function summarizeAudit(path: string): AuditSummary {
  const summary: AuditSummary = { rows: 0, malformedRows: 0, canonicalSignals: 0, causalDecisions: 0, shadowEvents: 0, explicitRejectsOrGaps: 0, eventTypes: {}, rejectReasons: {} };
  if (!existsSync(path)) return summary;
  for (const line of readFileSync(path, 'utf8').split('\n')) {
    if (!line.trim()) continue;
    summary.rows += 1;
    try {
      const row = JSON.parse(line);
      const type = typeof row?.type === 'string' ? row.type : 'missing_type';
      increment(summary.eventTypes, type);
      if (row?.signal) summary.canonicalSignals += 1;
      if (row?.signal && decisionTypes.has(type)) summary.causalDecisions += 1;
      if (shadowTypes.has(type)) summary.shadowEvents += 1;
      if (type === 'skip' || type === 'execution_error') {
        summary.explicitRejectsOrGaps += 1;
        increment(summary.rejectReasons, String(row?.reason ?? row?.error ?? type));
      }
    } catch {
      summary.malformedRows += 1;
    }
  }
  return summary;
}

function delta(current: number, baseline: number): number { return Math.max(0, current - baseline); }

export class Lane3AcceptanceEvidence {
  private lastWrittenAtMs = 0;

  constructor(private readonly latestPath: string, private readonly journalPath: string, private readonly auditPath: string, private readonly intervalMs: number) {}

  record(population: PopulationReport, live: boolean, nowMs = Date.now(), force = false): EvidenceSnapshot | null {
    if (live) throw new Error('Lane 3 runtime acceptance evidence is shadow-only');
    if (!force && nowMs - this.lastWrittenAtMs < this.intervalMs) return null;
    const audit = summarizeAudit(this.auditPath);
    let prior: EvidenceSnapshot | null = null;
    if (existsSync(this.latestPath)) {
      try { prior = JSON.parse(readFileSync(this.latestPath, 'utf8')) as EvidenceSnapshot; } catch { prior = null; }
    }
    const observedAt = new Date(nowMs).toISOString();
    const baseline = prior?.baseline ?? { funnel: { ...population.funnel }, audit };
    const eventCount = population.traders.reduce((n, trader) => n + trader.eventCount, 0);
    const observationDays = [...new Set(population.traders.flatMap(trader => trader.observationDays))].sort();
    const symbols = [...new Set(population.traders.flatMap(trader => trader.symbols))].sort();
    const missingOrFailedReasons: Record<string, number> = {};
    for (const trader of population.traders) for (const [reason, count] of Object.entries(trader.missingOrFailedReasons)) incrementBy(missingOrFailedReasons, reason, count);
    const growth = {
      discovered: delta(population.funnel.discovered ?? 0, baseline.funnel.discovered ?? 0),
      receivingNotifications: delta(population.funnel.receivingNotifications ?? 0, baseline.funnel.receivingNotifications ?? 0),
      shadowAssessable: delta(population.funnel.shadowAssessable ?? 0, baseline.funnel.shadowAssessable ?? 0),
      canonicalSignals: delta(audit.canonicalSignals, baseline.audit.canonicalSignals),
      causalDecisions: delta(audit.causalDecisions, baseline.audit.causalDecisions),
    };
    const checks = {
      realTradingDisabled: true as const,
      populationGrowing: growth.discovered > 0,
      freshCausalEvidence: growth.causalDecisions > 0,
      everyCanonicalSignalAccountedFor: growth.causalDecisions >= growth.canonicalSignals,
    };
    const snapshot: EvidenceSnapshot = {
      schemaVersion: 1, contractId: 'LANE3-RUNTIME-ACCEPTANCE-v1', observedAt,
      windowStartedAt: prior?.windowStartedAt ?? observedAt, live: false,
      policy: population.policy, baseline,
      current: { funnel: population.funnel, audit, eventCount, observationDays, symbols, missingOrFailedReasons, traders: population.traders },
      growth, checks,
      status: checks.populationGrowing && checks.freshCausalEvidence && checks.everyCanonicalSignalAccountedFor ? 'RUNTIME_ACTIVITY_PROVEN' : 'COLLECTING',
      promotionVerdict: 'NOT_EVALUATED',
    };
    mkdirSync(dirname(this.latestPath), { recursive: true });
    mkdirSync(dirname(this.journalPath), { recursive: true });
    const temp = `${this.latestPath}.tmp`;
    writeFileSync(temp, `${JSON.stringify(snapshot, null, 2)}\n`);
    renameSync(temp, this.latestPath);
    appendFileSync(this.journalPath, `${JSON.stringify(snapshot)}\n`);
    this.lastWrittenAtMs = nowMs;
    return snapshot;
  }
}

function incrementBy(target: Record<string, number>, key: string, count: number) {
  target[key] = (target[key] ?? 0) + count;
}
