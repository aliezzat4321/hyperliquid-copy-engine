import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'fs';
import { dirname, resolve } from 'path';
import { fileURLToPath } from 'url';
import { ALLOWED_PORTFOLIO_BUCKETS, ELITE_SELECTOR_VERSION, type PortfolioSnapshot } from './portfolio-candidates.js';
type Row = Record<string, any>;
type Membership = { sourceBaseId: string; portfolioId: string; selectedAtMs: number;
  selectorVersion: string; unresolvedAfterSourceClose: boolean };

function plain(value: unknown): value is Row {
  if (value == null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function jsonl(path: string, kind: 'selector' | 'audit'): any[] {
  if (!existsSync(path)) return [];
  return readFileSync(path, 'utf8').split(/\r?\n/).flatMap((line, index) => {
    if (!line.trim()) return [];
    try {
      const parsed = JSON.parse(line);
      if (!plain(parsed)) throw new Error(`${kind} row must be a plain object`);
      if (kind === 'selector') validateSelectorRow(parsed, `${path}:${index + 1}`);
      else validateAuditRow(parsed, `${path}:${index + 1}`);
      return [parsed];
    }
    catch (error) {
      throw new Error(`corrupt JSONL evidence ${path}:${index + 1}: ${error instanceof Error ? error.message : String(error)}`);
    }
  });
}

function validateSelectorRow(row: unknown, location = 'selector snapshot'): asserts row is PortfolioSnapshot {
  if (!plain(row)
    || typeof row.portfolioId !== 'string' || row.portfolioId.trim().length === 0
    || typeof row.observedAtMs !== 'number' || !Number.isFinite(row.observedAtMs) || row.observedAtMs <= 0
    || row.selectorVersion !== ELITE_SELECTOR_VERSION
    || !ALLOWED_PORTFOLIO_BUCKETS.has(row.bucket as any)) {
    throw new Error(`corrupt selector evidence ${location}`);
  }
}

const LIFECYCLE_TYPES = new Set([
  'shadow_opened', 'shadow_opened_from_increase', 'shadow_reupped', 'shadow_closed',
  'shadow_partially_closed', 'shadow_close_dust_reconciled', 'shadow_close_incomplete',
  'shadow_close_rejected',
]);

function validateAuditRow(row: unknown, location = 'audit row'): asserts row is Row {
  if (!plain(row)) throw new Error(`corrupt audit evidence ${location}: row must be a plain object`);
  const type = typeof row.type === 'string' ? row.type : '';
  if (!type.trim()) throw new Error(`corrupt audit evidence ${location}: type missing or invalid`);
  const looksLifecycle = type.startsWith('shadow_open') || type.startsWith('shadow_close')
    || type === 'shadow_reupped';
  if (looksLifecycle && !LIFECYCLE_TYPES.has(type)) {
    throw new Error(`corrupt audit evidence ${location}: unrecognized lifecycle type`);
  }
  if (LIFECYCLE_TYPES.has(type)) {
    if (at(row) == null) throw new Error(`corrupt audit evidence ${location}: lifecycle time missing or invalid`);
    if (!baseId(row).trim() || !portfolio(row).trim()) {
      throw new Error(`corrupt audit evidence ${location}: lifecycle identity missing`);
    }
  }
}

/** Merge immutable archive, previous, and hot segments; exact duplicates count once. */
export function loadCandidateSnapshots(hot: string): PortfolioSnapshot[] {
  const archive = `${hot}.archive`;
  const paths = existsSync(archive) && statSync(archive).isDirectory()
    ? readdirSync(archive).filter(n => /^segment-.*\.jsonl$/.test(n)).sort().map(n => `${archive}/${n}`) : [];
  paths.push(`${hot}.previous`, hot);
  const unique = new Map<string, PortfolioSnapshot>();
  for (const path of paths) for (const row of jsonl(path, 'selector')) {
    const key = JSON.stringify(row, Object.keys(row ?? {}).sort()); if (!unique.has(key)) unique.set(key, row);
  }
  return [...unique.values()].sort((a, b) => Number(a.observedAtMs) - Number(b.observedAtMs)
    || String(a.portfolioId).localeCompare(String(b.portfolioId)) || JSON.stringify(a).localeCompare(JSON.stringify(b)));
}
export function candidateSnapshotStorage(hot: string) {
  const archive = `${hot}.archive`;
  const segments = existsSync(archive) && statSync(archive).isDirectory()
    ? readdirSync(archive).filter(n => /^segment-.*\.jsonl$/.test(n)) : [];
  const archiveBytes = segments.reduce((sum, name) => sum + statSync(`${archive}/${name}`).size, 0);
  return { selectorArchiveSegments: segments.length, selectorArchiveBytes: archiveBytes,
    selectorPreviousBytes: existsSync(`${hot}.previous`) ? statSync(`${hot}.previous`).size : 0,
    selectorHotBytes: existsSync(hot) ? statSync(hot).size : 0,
    selectorArchiveRetentionPolicy: 'retain_all_causal_evidence_external_storage_guard_required' };
}
function at(row: Row): number | null { for (const value of [row.decisionAtMs, row.bookReceivedAtMs,
  row.observedAtMs, Date.parse(String(row.ts ?? ''))]) { const n = Number(value); if (Number.isFinite(n) && n > 0) return n; } return null; }
const baseId = (r: Row) => String(r.sourceBaseId ?? r.signal?.sourceBaseId ?? '');
const portfolio = (r: Row) => String(r.portfolioId ?? r.signal?.portfolioId ?? '');
const open = (t: string) => t === 'shadow_opened' || t === 'shadow_opened_from_increase';
const close = (t: string) => ['shadow_closed', 'shadow_partially_closed', 'shadow_close_dust_reconciled',
  'shadow_close_incomplete', 'shadow_close_rejected'].includes(t);
function histories(rows: PortfolioSnapshot[]) { const out = new Map<string, PortfolioSnapshot[]>();
  for (const [index, row] of rows.entries()) { validateSelectorRow(row, `projection row ${index + 1}`);
    const list = out.get(row.portfolioId) ?? []; list.push(row); out.set(row.portfolioId, list); }
  for (const list of out.values()) list.sort((a, b) => {
    const observed = a.observedAtMs - b.observedAtMs;
    if (observed) return observed;
    // Same-time conflicting selector evidence resolves conservatively: a non-elite
    // row sorts last, so it dominates rather than manufacturing elite membership.
    const aRank = a.bucket === 'ELITE_CANDIDATE' ? 0 : 1;
    const bRank = b.bucket === 'ELITE_CANDIDATE' ? 0 : 1;
    if (aRank !== bRank) return aRank - bRank;
    return JSON.stringify(a).localeCompare(JSON.stringify(b));
  }); return out; }
function selectedAt(all: Map<string, PortfolioSnapshot[]>, id: string, when: number) { let found: PortfolioSnapshot | null = null;
  for (const row of all.get(id) ?? []) { if (row.observedAtMs <= when) found = row; else break; } return found; }

export function projectEliteShadow(snapshots: PortfolioSnapshot[], audit: Row[], health: any | null) {
  const all = histories(snapshots); const active = new Map<string, Membership>(); const included: Row[] = [];
  const rejects: Row[] = []; const orphanEliteCloses: Row[] = []; let selectorSnapshotUnresolvedOpenEvents = 0;
  for (const [index, row] of audit.entries()) { validateAuditRow(row, `projection row ${index + 1}`);
    const when = at(row); if (when == null) continue;
    const id = baseId(row), pid = portfolio(row), type = String(row.type ?? ''), selected = pid ? selectedAt(all, pid, when) : null;
    if (open(type) && id && pid) { if (!selected) selectorSnapshotUnresolvedOpenEvents += 1;
      if (selected?.bucket === 'ELITE_CANDIDATE') active.set(id, { sourceBaseId: id, portfolioId: pid,
        selectedAtMs: selected.observedAtMs, selectorVersion: selected.selectorVersion, unresolvedAfterSourceClose: false }); }
    const membership = id ? active.get(id) : null;
    if (membership) { included.push({ ...row, elitePortfolioId: membership.portfolioId,
      eliteSelectedAtMs: membership.selectedAtMs, eliteSelectorVersion: membership.selectorVersion, cohort: 'elite_shadow' });
      if (['shadow_close_incomplete', 'shadow_close_rejected'].includes(type)
          || row.economicsCompleteness === 'UNRESOLVED_EXPOSURE') membership.unresolvedAfterSourceClose = true;
      if (type === 'shadow_closed' || type === 'shadow_close_dust_reconciled') active.delete(id); continue; }
    if (!membership && close(type) && pid) {
      const hadEliteBefore = (all.get(pid) ?? []).some(snapshot =>
        snapshot.observedAtMs <= when && snapshot.bucket === 'ELITE_CANDIDATE');
      if (hadEliteBefore) {
        orphanEliteCloses.push({ ...row, cohort: 'elite_shadow_unresolved_close',
          selectorMembershipResolved: false });
      }
    }
    if (['skip', 'close_ownership_gap', 'execution_error'].includes(type) && pid && selected?.bucket === 'ELITE_CANDIDATE')
      rejects.push({ ts: row.ts, type, reason: row.reason ?? null, sourceBaseId: id || null, portfolioId: pid,
        selectorSnapshotAtMs: selected.observedAtMs });
  }

  const healthAvailable = health != null && typeof health === 'object' && !Array.isArray(health);
  const healthOperational = healthAvailable
    && health.ok === true
    && health.live === false
    && health.shadowOperationalReady === true
    && Array.isArray(health.shadowOperationalFailures)
    && health.shadowOperationalFailures.length === 0;
  const healthOpenExposureCount = typeof health?.shadowOpenExposureCount === 'number'
    && Number.isInteger(health.shadowOpenExposureCount) && health.shadowOpenExposureCount >= 0
      ? health.shadowOpenExposureCount : null;
  const healthUnresolvedCount = typeof health?.unresolvedSourceCloseExposureCount === 'number'
    && Number.isInteger(health.unresolvedSourceCloseExposureCount) && health.unresolvedSourceCloseExposureCount >= 0
      ? health.unresolvedSourceCloseExposureCount : null;
  const managedObject = healthAvailable && plain(health.managed) ? health.managed : null;
  const managedRows = managedObject == null ? null : Object.entries(managedObject);
  let managedRowsValid = managedRows != null;
  const runtimePaperIds = new Set<string>();
  if (managedRows) for (const [, position] of managedRows) {
    if (!plain(position) || position.paper !== true || typeof position.sourceBaseId !== 'string'
      || position.sourceBaseId.trim().length === 0
      || runtimePaperIds.has(position.sourceBaseId)) {
      managedRowsValid = false;
      continue;
    }
    runtimePaperIds.add(position.sourceBaseId);
  }
  const markRows = Array.isArray(health?.shadowMarks) ? health.shadowMarks : null;
  const recognizedMarkStatuses = new Set(['MARKED', 'FUNDING_UNAVAILABLE', 'BOOK_REJECTED', 'INCOMPLETE_LEGACY_ENTRY']);
  let markRowsValid = markRows != null;
  const markIds = new Set<string>();
  if (markRows) for (const mark of markRows) {
    if (!plain(mark) || typeof mark.sourceBaseId !== 'string' || mark.sourceBaseId.trim().length === 0
      || markIds.has(mark.sourceBaseId) || !recognizedMarkStatuses.has(String(mark.status ?? ''))
      || (mark.status === 'MARKED' && (typeof mark.netPnlUsd !== 'number' || !Number.isFinite(mark.netPnlUsd)))) {
      markRowsValid = false;
      continue;
    }
    markIds.add(mark.sourceBaseId);
  }
  const runtimeHealthValid = healthOperational
    && healthOpenExposureCount != null
    && healthUnresolvedCount != null
    && managedRowsValid && runtimePaperIds.size === healthOpenExposureCount
    && markRowsValid;

  const ids = new Set(active.keys());
  const runtimeOnlyOpenExposureIds = managedRows == null ? [] : [...runtimePaperIds].filter(id => !ids.has(id)).sort();
  const auditOnlyOpenExposureIds = managedRows == null ? [...ids].sort() : [...ids].filter(id => !runtimePaperIds.has(id)).sort();
  const runtimeExposureCountMismatch = runtimeHealthValid
    ? Math.abs((healthOpenExposureCount ?? -1) - active.size) : null;
  const runtimeExposureReconciliationComplete = runtimeHealthValid
    && runtimeOnlyOpenExposureIds.length === 0
    && auditOnlyOpenExposureIds.length === 0
    && runtimeExposureCountMismatch === 0;

  const marks = new Map((markRows ?? [])
    .filter((m: any) => ids.has(String(m?.sourceBaseId ?? ''))).map((m: any) => [String(m.sourceBaseId), m]));
  const projectedUnresolvedSourceCloseExposureCount =
    [...active.values()].filter(m => m.unresolvedAfterSourceClose).length;
  const unresolvedSourceCloseExposureCount = runtimeHealthValid
    ? Math.max(projectedUnresolvedSourceCloseExposureCount, healthUnresolvedCount ?? 0)
    : projectedUnresolvedSourceCloseExposureCount;
  let markedElitePositions = 0, openMarkIncompletePositions = 0, openNet = 0;
  for (const membership of active.values()) { const mark: any = marks.get(membership.sourceBaseId);
    if (!membership.unresolvedAfterSourceClose && mark?.status === 'MARKED' && Number.isFinite(Number(mark.netPnlUsd))) {
      markedElitePositions += 1; openNet += Number(mark.netPnlUsd);
    } else openMarkIncompletePositions += 1; }
  const closes = included.filter(row => close(String(row.type ?? '')));
  const orphanEliteCloseEvents = orphanEliteCloses.length;
  const realized = closes.filter(row => row.type === 'shadow_closed'
    && row.economicsCompleteness === 'COMPLETE_EXECUTION_REALISTIC' && Number.isFinite(Number(row.netPnlUsd)));
  const realizedNetPnlUsd = realized.reduce((sum, row) => sum + Number(row.netPnlUsd), 0);
  const incomplete = closes.filter(row => row.type !== 'shadow_closed'
    || row.economicsCompleteness !== 'COMPLETE_EXECUTION_REALISTIC' || !Number.isFinite(Number(row.netPnlUsd)));
  const partialCloseEvents = closes.filter(r => r.type === 'shadow_partially_closed').length;
  const dustReconciledCloseEvents = closes.filter(r => r.type === 'shadow_close_dust_reconciled').length;
  const incompleteFundingCloses = closes.filter(r => r.economicsCompleteness === 'INCOMPLETE_FUNDING').length;
  const incompleteLegacyCloses = closes.filter(r => r.economicsCompleteness === 'INCOMPLETE_LEGACY_ENTRY').length;
  const unresolvedExposureCloses = closes.filter(r => r.economicsCompleteness === 'UNRESOLVED_EXPOSURE').length;
  const otherIncompleteCloses = incomplete.filter(r => r.type !== 'shadow_partially_closed'
    && r.type !== 'shadow_close_dust_reconciled' && !['INCOMPLETE_FUNDING', 'INCOMPLETE_LEGACY_ENTRY', 'UNRESOLVED_EXPOSURE']
      .includes(String(r.economicsCompleteness))).length;
  const openEconomicsComplete = runtimeExposureReconciliationComplete
    && openMarkIncompletePositions === 0 && unresolvedSourceCloseExposureCount === 0;
  const openNetPnlUsd = openEconomicsComplete ? openNet : null;
  const profitabilityComplete = healthAvailable && runtimeHealthValid && runtimeExposureReconciliationComplete
    && selectorSnapshotUnresolvedOpenEvents === 0 && orphanEliteCloseEvents === 0
    && openMarkIncompletePositions === 0 && unresolvedSourceCloseExposureCount === 0 && incomplete.length === 0;
  const openByPortfolio: Record<string, number> = {}; for (const m of active.values())
    openByPortfolio[m.portfolioId] = (openByPortfolio[m.portfolioId] ?? 0) + 1;
  return { selectorVersion: ELITE_SELECTOR_VERSION, methodology: 'causal_execution_realistic_elite_shadow',
    retroactiveSelectionForbidden: true, candidateSnapshotCount: snapshots.length, auditedResearchEvents: audit.length,
    eliteLifecycleEvents: included.length, eliteCandidateRejects: rejects.length, selectorSnapshotUnresolvedOpenEvents,
    openElitePositions: active.size, openEliteByPortfolio: openByPortfolio, markedElitePositions,
    openMarkIncompletePositions, unresolvedSourceCloseExposureCount,
    runtimeHealthValid, runtimeExposureReconciliationComplete,
    runtimeOnlyOpenExposureCount: runtimeOnlyOpenExposureIds.length,
    auditOnlyOpenExposureCount: auditOnlyOpenExposureIds.length,
    runtimeExposureCountMismatch,
    runtimeOnlyOpenExposureIds: runtimeOnlyOpenExposureIds.slice(0, 50),
    auditOnlyOpenExposureIds: auditOnlyOpenExposureIds.slice(0, 50),
    totalCloseEvents: closes.length + orphanEliteCloseEvents, orphanEliteCloseEvents,
    realizedCompleteCloses: realized.length, partialCloseEvents, dustReconciledCloseEvents, incompleteFundingCloses,
    incompleteLegacyCloses, unresolvedExposureCloses, otherIncompleteCloses,
    excludedIncompleteCloses: incomplete.length + orphanEliteCloseEvents,
    realizedNetPnlUsd, openNetPnlUsd,
    totalObservedNetPnlUsd: profitabilityComplete && openNetPnlUsd != null ? realizedNetPnlUsd + openNetPnlUsd : null,
    profitabilityComplete, healthAvailable, liveTrading: false, candidateRejectSamples: rejects.slice(-50),
    unresolvedLifecycleSamples: orphanEliteCloses.slice(-50), included };
}
async function fetchHealth(url: string) { try { const r = await fetch(url, { signal: AbortSignal.timeout(70_000) });
  return r.ok ? await r.json() : null; } catch { return null; } }
export function writeEliteShadowReport(snapshotPath: string, auditPath: string, output: string,
  ledger: string, health: any | null) {
  const report = projectEliteShadow(loadCandidateSnapshots(snapshotPath), jsonl(auditPath, 'audit'), health);
  const { included, ...summary } = report;
  const rendered = { generatedAtMs: Date.now(), ...summary, ...candidateSnapshotStorage(snapshotPath) };
  mkdirSync(dirname(output), { recursive: true }); mkdirSync(dirname(ledger), { recursive: true });
  writeFileSync(output, JSON.stringify(rendered, null, 2)); writeFileSync(ledger, included.map(row => JSON.stringify(row)).join('\n') + (included.length ? '\n' : ''));
  return rendered;
}
async function main() { const snapshotPath = resolve(process.env.INVO_PORTFOLIO_CANDIDATE_SNAPSHOTS_PATH ?? 'data/portfolio-candidate-snapshots.jsonl');
  const output = resolve(process.env.INVO_ELITE_SHADOW_REPORT_PATH ?? 'data/elite-shadow-report.json');
  const ledger = resolve(process.env.INVO_ELITE_SHADOW_LEDGER_PATH ?? 'data/elite-shadow-ledger.jsonl');
  const rendered = writeEliteShadowReport(snapshotPath,
    resolve(process.env.NOTIFICATION_TRADER_AUDIT_PATH ?? 'data/notification-trader-audit.jsonl'), output, ledger,
    await fetchHealth(process.env.INVO_ELITE_SHADOW_HEALTH_URL ?? 'http://127.0.0.1:8787/health'));
  console.log(JSON.stringify(rendered, null, 2)); }
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch(error => {
  console.error(JSON.stringify({ ok: false, error: error instanceof Error ? error.message : String(error) })); process.exit(1); });
