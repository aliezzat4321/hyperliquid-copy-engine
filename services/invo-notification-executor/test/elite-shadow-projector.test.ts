import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { loadCandidateSnapshots, projectEliteShadow } from '../src/elite-shadow-projector.js';
import { ELITE_SELECTOR_VERSION } from '../src/portfolio-candidates.js';

const snap = (portfolioId: string, observedAtMs: number) => ({ portfolioId, observedAtMs,
  bucket: 'ELITE_CANDIDATE', selectorVersion: ELITE_SELECTOR_VERSION } as any);
const opened = (id: string, portfolioId: string, decisionAtMs: number) => ({ type: 'shadow_opened',
  sourceBaseId: id, portfolioId, decisionAtMs });
const health = (managed: Record<string, any> = {}, marks: any[] = [], unresolved = 0) => ({
  shadowMarks: marks,
  shadowOpenExposureCount: Object.values(managed).filter((p: any) => p?.paper === true).length,
  unresolvedSourceCloseExposureCount: unresolved,
  managed,
});

test('archive, previous, and current resolve rotated open membership with exact duplicate dedupe', () => {
  const dir = mkdtempSync(join(tmpdir(), 'selector-')); const hot = join(dir, 'snapshots.jsonl');
  mkdirSync(`${hot}.archive`); const old = snap('p1', 100);
  writeFileSync(`${hot}.archive/segment-2.jsonl`, `${JSON.stringify(old)}\n`);
  writeFileSync(`${hot}.previous`, `${JSON.stringify(old)}\n${JSON.stringify(snap('p2', 200))}\n`);
  writeFileSync(hot, `${JSON.stringify(snap('p3', 300))}\n`);
  const snapshots = loadCandidateSnapshots(hot); assert.equal(snapshots.length, 3);
  const report = projectEliteShadow(snapshots, [opened('x', 'p1', 150), {
    type: 'shadow_closed', sourceBaseId: 'x', portfolioId: 'p1', decisionAtMs: 400,
    economicsCompleteness: 'COMPLETE_EXECUTION_REALISTIC', netPnlUsd: 7,
  }], health());
  assert.equal(report.realizedNetPnlUsd, 7); assert.equal(report.realizedCompleteCloses, 1);
  assert.equal(report.selectorSnapshotUnresolvedOpenEvents, 0);
});

test('unknown marks and unresolved source-close exposure null headline open and total PnL', () => {
  for (const mark of [undefined, { status: 'FUNDING_UNAVAILABLE' }, { status: 'BOOK_REJECTED' },
    { status: 'INCOMPLETE_LEGACY_ENTRY' }, { status: 'MARKED', netPnlUsd: Number.NaN }]) {
    const report = projectEliteShadow([snap('p', 100)], [opened('x', 'p', 200)],
      health({ x: { paper: true, sourceBaseId: 'x' } }, mark ? [{ sourceBaseId: 'x', ...mark }] : []));
    assert.equal(report.openNetPnlUsd, null); assert.equal(report.totalObservedNetPnlUsd, null);
    assert.equal(report.openMarkIncompletePositions, 1); assert.equal(report.profitabilityComplete, false);
  }
  const unresolved = projectEliteShadow([snap('p', 100)], [opened('x', 'p', 200), {
    type: 'shadow_close_rejected', sourceBaseId: 'x', portfolioId: 'p', decisionAtMs: 300,
    economicsCompleteness: 'UNRESOLVED_EXPOSURE',
  }], health({ x: { paper: true, sourceBaseId: 'x' } },
    [{ sourceBaseId: 'x', status: 'MARKED', netPnlUsd: 3 }], 1));
  assert.equal(unresolved.unresolvedSourceCloseExposureCount, 1); assert.equal(unresolved.openNetPnlUsd, null);
});

test('every incomplete close remains explicit and unresolved selector opens cannot vanish', () => {
  const rows: any[] = [opened('a', 'p', 200),
    { type: 'shadow_partially_closed', sourceBaseId: 'a', portfolioId: 'p', decisionAtMs: 210,
      economicsCompleteness: 'COMPLETE_EXECUTION_REALISTIC', netPnlUsd: 1 },
    { type: 'shadow_close_rejected', sourceBaseId: 'a', portfolioId: 'p', decisionAtMs: 220,
      economicsCompleteness: 'INCOMPLETE_FUNDING' },
    { type: 'shadow_close_rejected', sourceBaseId: 'a', portfolioId: 'p', decisionAtMs: 230,
      economicsCompleteness: 'INCOMPLETE_LEGACY_ENTRY' },
    { type: 'shadow_close_dust_reconciled', sourceBaseId: 'a', portfolioId: 'p', decisionAtMs: 240,
      economicsCompleteness: 'INCOMPLETE_DUST_RECONCILIATION' }, opened('missing', 'unknown', 250)];
  const report = projectEliteShadow([snap('p', 100)], rows, health());
  assert.equal(report.totalCloseEvents, 4); assert.equal(report.realizedCompleteCloses, 0);
  assert.equal(report.partialCloseEvents, 1); assert.equal(report.dustReconciledCloseEvents, 1);
  assert.equal(report.incompleteFundingCloses, 1); assert.equal(report.incompleteLegacyCloses, 1);
  assert.equal(report.excludedIncompleteCloses, 4); assert.equal(report.selectorSnapshotUnresolvedOpenEvents, 1);
  assert.equal(report.profitabilityComplete, false);
});

test('same-timestamp conflicting selector evidence resolves non-elite conservatively', () => {
  const snapshots: any[] = [
    { ...snap('p', 100), bucket: 'ELITE_CANDIDATE', sourceFilter: 'a' },
    { ...snap('p', 100), bucket: 'RESEARCH_WIDE', sourceFilter: 'b' },
  ];
  const report = projectEliteShadow(snapshots, [opened('x', 'p', 150)], health());
  assert.equal(report.openElitePositions, 0);
  assert.equal(report.selectorSnapshotUnresolvedOpenEvents, 0);
});

test('runtime unresolved exposure count conservatively invalidates profitability even if audit missed the row', () => {
  const report = projectEliteShadow([snap('p', 100)], [opened('x', 'p', 200)],
    health({ x: { paper: true, sourceBaseId: 'x' } },
      [{ sourceBaseId: 'x', status: 'MARKED', netPnlUsd: 3 }], 1));
  assert.equal(report.unresolvedSourceCloseExposureCount, 1);
  assert.equal(report.openNetPnlUsd, null);
  assert.equal(report.totalObservedNetPnlUsd, null);
  assert.equal(report.profitabilityComplete, false);
});

test('missing or malformed runtime health is fatal to profitability completeness', () => {
  for (const runtime of [null, {}, { shadowMarks: [] },
    { shadowMarks: [], shadowOpenExposureCount: 0, unresolvedSourceCloseExposureCount: 0 }]) {
    const report = projectEliteShadow([snap('p', 100)], [], runtime as any);
    assert.equal(report.profitabilityComplete, false);
    assert.equal(report.openNetPnlUsd, null);
    assert.equal(report.totalObservedNetPnlUsd, null);
  }
});

test('runtime-only and audit-only open exposure cannot disappear from profitability', () => {
  const runtimeOnly = projectEliteShadow([snap('p', 100)], [],
    health({ r: { paper: true, sourceBaseId: 'runtime-only' } },
      [{ sourceBaseId: 'runtime-only', status: 'MARKED', netPnlUsd: 5 }]));
  assert.equal(runtimeOnly.runtimeOnlyOpenExposureCount, 1);
  assert.equal(runtimeOnly.profitabilityComplete, false);
  assert.equal(runtimeOnly.openNetPnlUsd, null);

  const auditOnly = projectEliteShadow([snap('p', 100)], [opened('audit-only', 'p', 200)], health());
  assert.equal(auditOnly.auditOnlyOpenExposureCount, 1);
  assert.equal(auditOnly.profitabilityComplete, false);
  assert.equal(auditOnly.openNetPnlUsd, null);
});

test('orphan elite close is explicit in denominator and invalidates profitability', () => {
  const report = projectEliteShadow([snap('p', 100)], [{
    type: 'shadow_closed', sourceBaseId: 'missing-open', portfolioId: 'p', decisionAtMs: 300,
    economicsCompleteness: 'COMPLETE_EXECUTION_REALISTIC', netPnlUsd: 9,
  }], health());
  assert.equal(report.orphanEliteCloseEvents, 1);
  assert.equal(report.totalCloseEvents, 1);
  assert.equal(report.excludedIncompleteCloses, 1);
  assert.equal(report.realizedCompleteCloses, 0);
  assert.equal(report.realizedNetPnlUsd, 0);
  assert.equal(report.profitabilityComplete, false);
});
