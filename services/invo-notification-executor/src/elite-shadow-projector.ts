import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs';
import { dirname, resolve } from 'path';
import { ELITE_SELECTOR_VERSION, type PortfolioSnapshot } from './portfolio-candidates.js';

type AuditRow = Record<string, any>;

type EliteMembership = {
  sourceBaseId: string;
  portfolioId: string;
  username: string | null;
  selectedAtMs: number;
  selectorVersion: string;
};

function parseJsonl(path: string): any[] {
  if (!existsSync(path)) return [];
  return readFileSync(path, 'utf8')
    .split(/\r?\n/)
    .filter(Boolean)
    .map(line => {
      try { return JSON.parse(line); } catch { return null; }
    })
    .filter(Boolean);
}

function eventAtMs(row: AuditRow): number | null {
  const candidates = [row.decisionAtMs, row.bookReceivedAtMs, row.observedAtMs, Date.parse(String(row.ts ?? ''))];
  for (const value of candidates) {
    const n = Number(value);
    if (Number.isFinite(n) && n > 0) return n;
  }
  return null;
}

function sourceBaseId(row: AuditRow): string {
  return String(row.sourceBaseId ?? row.signal?.sourceBaseId ?? '');
}

function portfolioId(row: AuditRow): string {
  return String(row.portfolioId ?? row.signal?.portfolioId ?? '');
}

function buildHistories(snapshots: PortfolioSnapshot[]) {
  const histories = new Map<string, PortfolioSnapshot[]>();
  for (const snapshot of snapshots) {
    if (!snapshot?.portfolioId || !Number.isFinite(snapshot.observedAtMs)) continue;
    const list = histories.get(snapshot.portfolioId) ?? [];
    list.push(snapshot);
    histories.set(snapshot.portfolioId, list);
  }
  for (const list of histories.values()) list.sort((a, b) => a.observedAtMs - b.observedAtMs);
  return histories;
}

function selectionAt(histories: Map<string, PortfolioSnapshot[]>, id: string, atMs: number): PortfolioSnapshot | null {
  const list = histories.get(id);
  if (!list?.length) return null;
  let lo = 0;
  let hi = list.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid].observedAtMs <= atMs) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found >= 0 ? list[found] : null;
}

function isOpenEvent(type: string) {
  return type === 'shadow_opened' || type === 'shadow_opened_from_increase';
}

function isCloseEvent(type: string) {
  return type === 'shadow_closed' || type === 'shadow_partially_closed' || type === 'shadow_close_dust_reconciled';
}

async function fetchHealth(url: string): Promise<any | null> {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(70_000) });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

async function main() {
  const snapshotsPath = resolve(process.env.INVO_PORTFOLIO_CANDIDATE_SNAPSHOTS_PATH ?? 'data/portfolio-candidate-snapshots.jsonl');
  const auditPath = resolve(process.env.NOTIFICATION_TRADER_AUDIT_PATH ?? 'data/notification-trader-audit.jsonl');
  const outputPath = resolve(process.env.INVO_ELITE_SHADOW_REPORT_PATH ?? 'data/elite-shadow-report.json');
  const ledgerPath = resolve(process.env.INVO_ELITE_SHADOW_LEDGER_PATH ?? 'data/elite-shadow-ledger.jsonl');
  const healthUrl = process.env.INVO_ELITE_SHADOW_HEALTH_URL ?? 'http://127.0.0.1:8787/health';

  const snapshots = parseJsonl(snapshotsPath) as PortfolioSnapshot[];
  const histories = buildHistories(snapshots);
  const audit = parseJsonl(auditPath) as AuditRow[];
  const memberships = new Map<string, EliteMembership>();
  const included: any[] = [];
  const candidateRejects: any[] = [];

  for (const row of audit) {
    const atMs = eventAtMs(row);
    if (atMs == null) continue;
    const baseId = sourceBaseId(row);
    const pId = portfolioId(row);
    const type = String(row.type ?? '');
    const selected = pId ? selectionAt(histories, pId, atMs) : null;

    if (isOpenEvent(type) && baseId && pId) {
      if (selected?.bucket === 'ELITE_CANDIDATE') {
        memberships.set(baseId, {
          sourceBaseId: baseId,
          portfolioId: pId,
          username: String(row.signal?.username ?? selected.username ?? '') || null,
          selectedAtMs: selected.observedAtMs,
          selectorVersion: selected.selectorVersion,
        });
      }
    }

    const membership = baseId ? memberships.get(baseId) : null;
    if (membership) {
      included.push({
        ...row,
        elitePortfolioId: membership.portfolioId,
        eliteSelectedAtMs: membership.selectedAtMs,
        eliteSelectorVersion: membership.selectorVersion,
        cohort: 'elite_shadow',
      });
      if (isCloseEvent(type) && type !== 'shadow_partially_closed') memberships.delete(baseId);
      continue;
    }

    if ((type === 'skip' || type === 'close_ownership_gap' || type === 'execution_error') && pId && selected?.bucket === 'ELITE_CANDIDATE') {
      candidateRejects.push({
        ts: row.ts,
        type,
        reason: row.reason ?? null,
        sourceBaseId: baseId || null,
        portfolioId: pId,
        username: row.signal?.username ?? selected.username ?? null,
        selectorSnapshotAtMs: selected.observedAtMs,
      });
    }
  }

  const health = await fetchHealth(healthUrl);
  const activeEliteBaseIds = new Set(memberships.keys());
  const marks = Array.isArray(health?.shadowMarks)
    ? health.shadowMarks.filter((mark: any) => activeEliteBaseIds.has(String(mark?.sourceBaseId ?? '')))
    : [];
  const realizedRows = included.filter(row => row.type === 'shadow_closed' && row.economicsCompleteness === 'COMPLETE_EXECUTION_REALISTIC' && Number.isFinite(Number(row.netPnlUsd)));
  const realizedNetPnlUsd = realizedRows.reduce((sum, row) => sum + Number(row.netPnlUsd), 0);
  const openNetPnlUsd = marks.reduce((sum: number, mark: any) => sum + (Number.isFinite(Number(mark?.netPnlUsd)) ? Number(mark.netPnlUsd) : 0), 0);
  const markedCount = marks.filter((mark: any) => mark?.status === 'MARKED').length;
  const openByPortfolio: Record<string, number> = {};
  for (const membership of memberships.values()) openByPortfolio[membership.portfolioId] = (openByPortfolio[membership.portfolioId] ?? 0) + 1;

  const report = {
    generatedAtMs: Date.now(),
    selectorVersion: ELITE_SELECTOR_VERSION,
    methodology: 'causal_projection_of_execution_realistic_research_wide_shadow',
    causalRule: 'a source position enters elite_shadow only when its portfolio was already ELITE_CANDIDATE before the opening shadow decision; membership remains through the position lifecycle so closes cannot be censored',
    retroactiveSelectionForbidden: true,
    candidateSnapshotCount: snapshots.length,
    auditedResearchEvents: audit.length,
    eliteLifecycleEvents: included.length,
    eliteCandidateRejects: candidateRejects.length,
    openElitePositions: memberships.size,
    openEliteByPortfolio: openByPortfolio,
    markedElitePositions: markedCount,
    realizedCompleteCloses: realizedRows.length,
    realizedNetPnlUsd,
    openNetPnlUsd,
    totalObservedNetPnlUsd: realizedNetPnlUsd + openNetPnlUsd,
    healthAvailable: Boolean(health),
    liveTrading: false,
    candidateRejectSamples: candidateRejects.slice(-50),
  };

  mkdirSync(dirname(outputPath), { recursive: true });
  mkdirSync(dirname(ledgerPath), { recursive: true });
  writeFileSync(outputPath, JSON.stringify(report, null, 2));
  writeFileSync(ledgerPath, included.map(row => JSON.stringify(row)).join('\n') + (included.length ? '\n' : ''));
  console.log(JSON.stringify(report, null, 2));
}

main().catch(err => {
  console.error(JSON.stringify({ ok: false, error: err instanceof Error ? err.message : String(err) }));
  process.exit(1);
});
