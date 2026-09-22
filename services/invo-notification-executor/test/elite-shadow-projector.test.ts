import assert from 'node:assert/strict';
import test from 'node:test';
import { closeSync, fsyncSync, mkdirSync, mkdtempSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { loadCandidateSnapshots, projectEliteShadow, readEliteShadowPublication, writeEliteShadowReport,
  type ElitePublicationIo } from '../src/elite-shadow-projector.js';
import { ELITE_SELECTOR_VERSION } from '../src/portfolio-candidates.js';

const snap = (portfolioId: string, observedAtMs: number) => ({ portfolioId, observedAtMs,
  bucket: 'ELITE_CANDIDATE', selectorVersion: ELITE_SELECTOR_VERSION } as any);
const opened = (id: string, portfolioId: string, decisionAtMs: number) => ({ type: 'shadow_opened',
  sourceBaseId: id, portfolioId, decisionAtMs });
const managedPosition = (sourceBaseId: string, override: Record<string, any> = {}) => ({
  coin: 'BTC', sourcePostId: `post-${sourceBaseId}`, sourceBaseShortId: `short-${sourceBaseId}`,
  sourceBaseId, side: 'long', paper: true, openedAtMs: 100, ...override,
});
const shadowMark = (sourceBaseId: string, override: Record<string, any> = {}) => {
  const status = override.status ?? 'MARKED';
  const common: any = { sourceBaseId, coin: 'BTC', side: 'long', size: 1, status, markedAtMs: 300,
    executionEvidenceVersion: 'lane3-causal-l2-v2', costModelVersion: 'hl-taker-l2-oracle-funding-v2' };
  if (status === 'MARKED' || status === 'PARTIAL_DEPTH') Object.assign(common, { entryPrice: 100, markExitPrice: 101,
    markedSize: 1, unfilledSize: 0, grossPnlUsd: 1, netPnlUsd: 0.8, grossReturnBps: 100,
    netReturnBps: 80, fundingUsd: 0.01, entryFeeUsd: 0.05, exitFeeUsd: 0.05, spreadBps: 1,
    slippageBps: 2, bookAgeMs: 3, bookTimeMs: 297 });
  else common.reason = 'incomplete evidence';
  return { ...common, ...override };
};
const health = (managed: Record<string, any> = {}, marks: any[] = [], unresolved = 0) => ({
  ok: true, live: false, shadowOperationalReady: true, shadowOperationalFailures: [],
  shadowMarks: marks.map(mark => mark?.sourceBaseId ? shadowMark(mark.sourceBaseId, mark) : mark),
  shadowOpenExposureCount: Object.values(managed).filter((p: any) => p?.paper === true).length,
  unresolvedSourceCloseExposureCount: unresolved,
  managed: Object.fromEntries(Object.entries(managed).map(([key, value]) =>
    [key, value?.sourceBaseId ? managedPosition(value.sourceBaseId, value) : value])),
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
    { shadowMarks: [], shadowOpenExposureCount: 0, unresolvedSourceCloseExposureCount: 0 },
    { ...health(), shadowOpenExposureCount: null },
    { ...health(), unresolvedSourceCloseExposureCount: '' },
    { ...health(), ok: false },
    { ...health(), live: true },
    { ...health(), shadowOperationalReady: false, shadowOperationalFailures: ['unhealthy'] }]) {
    const report = projectEliteShadow([snap('p', 100)], [], runtime as any);
    assert.equal(report.profitabilityComplete, false);
    assert.equal(report.openNetPnlUsd, null);
    assert.equal(report.totalObservedNetPnlUsd, null);
  }
});

test('runtime-only and audit-only open exposure cannot disappear from profitability', () => {
  const runtimeOnly = projectEliteShadow([snap('p', 100)], [],
    health({ 'runtime-only': { paper: true, sourceBaseId: 'runtime-only' } },
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

test('malformed selector JSONL fails closed instead of preserving stale elite membership', () => {
  const root = mkdtempSync(join(tmpdir(), 'elite-shadow-corrupt-selector-'));
  const hot = join(root, 'snapshots.jsonl');
  writeFileSync(hot, JSON.stringify({ portfolioId: 'p1', observedAtMs: 1000, selectorVersion: ELITE_SELECTOR_VERSION, bucket: 'ELITE_CANDIDATE' }) + String.fromCharCode(10) + '{"portfolioId":"p1","observedAtMs":2000,');
  assert.throws(() => loadCandidateSnapshots(hot), /corrupt JSONL evidence/);
});

test('parseable malformed selector rows fail closed with explicit path and line', () => {
  const invalid = [
    {}, [], null, 7,
    { observedAtMs: 1, selectorVersion: ELITE_SELECTOR_VERSION, bucket: 'ELITE_CANDIDATE' },
    { portfolioId: 'p', selectorVersion: ELITE_SELECTOR_VERSION, bucket: 'ELITE_CANDIDATE' },
    { portfolioId: 'p', observedAtMs: 1, bucket: 'ELITE_CANDIDATE' },
    { portfolioId: 'p', observedAtMs: 1, selectorVersion: 'old', bucket: 'ELITE_CANDIDATE' },
    { portfolioId: 'p', observedAtMs: 1, selectorVersion: ELITE_SELECTOR_VERSION, bucket: 'UNKNOWN' },
  ];
  for (const [index, row] of invalid.entries()) {
    const root = mkdtempSync(join(tmpdir(), `elite-shadow-selector-schema-${index}-`));
    const path = join(root, 'snapshots.jsonl');
    writeFileSync(path, `${JSON.stringify(snap('valid', 1))}\n${JSON.stringify(row)}\n`);
    assert.throws(() => loadCandidateSnapshots(path), new RegExp(`corrupt JSONL evidence .*snapshots.jsonl:2`));
  }
});

test('lifecycle audit rows require recognized type, identity, and causal time', () => {
  const malformed = [
    {}, [], null, 1,
    { type: 'shadow_opened', sourceBaseId: 'x', portfolioId: 'p' },
    { type: 'shadow_closed', sourceBaseId: 'x', portfolioId: 'p' },
    { type: 'shadow_close_incomplete', sourceBaseId: 'x', portfolioId: 'p', decisionAtMs: Number.NaN },
    { type: 'shadow_close_unknown', sourceBaseId: 'x', portfolioId: 'p', decisionAtMs: 2 },
    { type: 'shadow_close_rejected', sourceBaseId: '', portfolioId: 'p', decisionAtMs: 2 },
  ];
  for (const row of malformed) {
    assert.throws(() => projectEliteShadow([snap('p', 1)], [row as any], health()), /corrupt audit evidence/);
  }
});

test('corrupt runtime managed and mark rows invalidate all headline profitability', () => {
  const corruptHealth = [
    health({ corrupt: {} }),
    health({}, [{}]),
    { ...health({ a: { paper: true, sourceBaseId: 'dup' }, b: { paper: true, sourceBaseId: 'dup' } }),
      shadowOpenExposureCount: 2 },
    { ...health(), shadowMarks: [
      { sourceBaseId: 'dup', status: 'BOOK_REJECTED' }, { sourceBaseId: 'dup', status: 'FUNDING_UNAVAILABLE' },
    ] },
    health({}, [{ sourceBaseId: 'x', status: 'MARKED', netPnlUsd: '4' }]),
    health({}, [{ sourceBaseId: 'x', status: 'UNKNOWN' }]),
    { ...health({ x: { paper: false, sourceBaseId: 'x' } }), shadowOpenExposureCount: 0 },
  ];
  for (const runtime of corruptHealth) {
    const report = projectEliteShadow([snap('p', 1)], [], runtime as any);
    assert.equal(report.runtimeHealthValid, false);
    assert.equal(report.profitabilityComplete, false);
    assert.equal(report.openNetPnlUsd, null);
    assert.equal(report.totalObservedNetPnlUsd, null);
  }
});

test('managed runtime rows require the complete canonical producer schema', () => {
  const valid = managedPosition('x');
  const corrupt: any[] = [];
  for (const field of ['coin', 'sourcePostId', 'sourceBaseShortId', 'sourceBaseId', 'side', 'paper', 'openedAtMs']) {
    const row: any = { ...valid }; delete row[field]; corrupt.push(row);
  }
  corrupt.push({ ...valid, coin: 1 }, { ...valid, side: 'up' }, { ...valid, openedAtMs: '100' },
    { ...valid, exposureCheckpoints: [{ atMs: '1', size: 1 }] },
    { ...valid, fundingOracleCheckpoints: [{ fundingTimeMs: 1, observedAtMs: 2, oraclePx: '3' }] });
  for (const row of corrupt) {
    const report = projectEliteShadow([snap('p', 1)], [], { ...health(), managed: { x: row }, shadowOpenExposureCount: 1 });
    assert.equal(report.runtimeHealthValid, false); assert.equal(report.totalObservedNetPnlUsd, null);
  }
});

test('shadow marks require complete common and status-specific producer fields', () => {
  const valid = shadowMark('x');
  const corrupt: any[] = [];
  for (const field of ['coin', 'side', 'size', 'markedAtMs', 'executionEvidenceVersion', 'costModelVersion',
    'entryPrice', 'markExitPrice', 'markedSize', 'netPnlUsd']) {
    const row = { ...valid }; delete row[field]; corrupt.push(row);
  }
  corrupt.push({ ...valid, size: '1' }, { ...valid, markedAtMs: '300' }, { ...valid, netPnlUsd: '0.8' },
    { ...valid, status: 'UNKNOWN' });
  for (const row of corrupt) {
    const report = projectEliteShadow([snap('p', 1)], [], { ...health(), shadowMarks: [row] });
    assert.equal(report.runtimeHealthValid, false); assert.equal(report.totalObservedNetPnlUsd, null);
  }
});

test('lifecycle identities times and complete economics are never coerced', () => {
  for (const row of [
    { type: 'shadow_opened', sourceBaseId: {}, portfolioId: 'p', decisionAtMs: 200 },
    { type: 'shadow_opened', sourceBaseId: 'x', portfolioId: true, decisionAtMs: 200 },
    { type: 'shadow_opened', sourceBaseId: 'x', portfolioId: 'p', decisionAtMs: '200' },
    { type: 'shadow_opened', sourceBaseId: 'x', portfolioId: 'p', decisionAtMs: true },
    { type: 'shadow_closed', sourceBaseId: 'x', portfolioId: 'p', decisionAtMs: 200,
      economicsCompleteness: {}, netPnlUsd: 1 },
    ...['7', null, Number.NaN, Number.POSITIVE_INFINITY].map(netPnlUsd => ({ type: 'shadow_closed',
      sourceBaseId: 'x', portfolioId: 'p', decisionAtMs: 200,
      economicsCompleteness: 'COMPLETE_EXECUTION_REALISTIC', netPnlUsd })),
  ]) assert.throws(() => projectEliteShadow([snap('p', 1)], [row as any], health()), /corrupt audit evidence/);
});

test('report generation corruption failure preserves the previous report', () => {
  const root = mkdtempSync(join(tmpdir(), 'elite-shadow-cli-fail-'));
  const snapshots = join(root, 'snapshots.jsonl');
  const audit = join(root, 'audit.jsonl');
  const report = join(root, 'report.json');
  const ledger = join(root, 'ledger.jsonl');
  writeFileSync(snapshots, `${JSON.stringify(snap('p', 1))}\n`);
  writeFileSync(audit, `${JSON.stringify({ type: 'shadow_opened', sourceBaseId: 'x', portfolioId: 'p' })}\n`);
  writeFileSync(report, 'prior-report');
  assert.throws(() => writeEliteShadowReport(snapshots, audit, report, ledger, health()),
    /corrupt JSONL evidence .*audit.jsonl:1/);
  assert.equal(readFileSync(report, 'utf8'), 'prior-report');
});

test('atomic generation manifest preserves the previous coherent pair on every publication failure stage', () => {
  const root = mkdtempSync(join(tmpdir(), 'elite-shadow-atomic-'));
  const snapshots = join(root, 'snapshots.jsonl'); const audit = join(root, 'audit.jsonl');
  const report = join(root, 'report.json'); const ledger = join(root, 'ledger.jsonl');
  writeFileSync(snapshots, `${JSON.stringify(snap('p', 1))}\n`); writeFileSync(audit, '');
  writeEliteShadowReport(snapshots, audit, report, ledger, health());
  const prior = readEliteShadowPublication(report);
  const baseIo: ElitePublicationIo = {
    write: (path, data) => writeFileSync(path, data),
    fsyncFile: path => { const fd = openSync(path, 'r'); try { fsyncSync(fd); } finally { closeSync(fd); } },
    rename: (from, to) => renameSync(from, to),
    fsyncDirectory: path => { const fd = openSync(path, 'r'); try { fsyncSync(fd); } finally { closeSync(fd); } },
    remove: path => { try { unlinkSync(path); } catch {} },
  };
  for (const [method, failAt] of [['write', 1], ['write', 2], ['fsyncFile', 1], ['rename', 1], ['rename', 2], ['rename', 3]] as const) {
    let calls = 0;
    const failing = { ...baseIo, [method]: (...args: any[]) => {
      calls += 1; if (calls === failAt) throw new Error(`injected ${method} failure`);
      return (baseIo[method] as any)(...args);
    } } as ElitePublicationIo;
    assert.throws(() => writeEliteShadowReport(snapshots, audit, report, ledger, health(), failing), /injected/);
    const current = readEliteShadowPublication(report);
    assert.equal(current.manifest.generationId, prior.manifest.generationId);
    assert.deepEqual(current.report, prior.report); assert.equal(current.ledger, prior.ledger);
  }
});
