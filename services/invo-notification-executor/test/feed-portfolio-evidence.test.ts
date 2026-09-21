import assert from 'node:assert/strict';
import test from 'node:test';
import { existsSync, mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  FeedPortfolioEvidenceStore,
  FEED_EVIDENCE_MAX_JOURNAL_BYTES,
  FEED_EVIDENCE_MAX_PORTFOLIOS,
  FEED_EVIDENCE_MAX_STATE_BYTES,
  FEED_EVIDENCE_SELECTOR_TTL_MS,
  loadFeedPortfolioEvidence,
  normalizeFeedPortfolioObservation,
} from '../src/feed-portfolio-evidence.js';
import { INVO_FEED_SURFACES } from '../src/feed-surfaces.js';
import { classifyPortfolio, PortfolioCandidateLedger } from '../src/portfolio-candidates.js';
import { eliteAdmissionFromState } from '../src/elite-admission.js';
import { EliteDirectWatchState, loadEliteDirectTargets } from '../src/elite-direct-watch.js';

const fixture = JSON.parse(readFileSync(
  new URL('../../test/fixtures/invo-feed-portfolio-evidence-captured-shapes.json', import.meta.url), 'utf8',
));
const capturedAtMs = Date.parse('2026-09-20T10:00:00Z');
const processedAtMs = capturedAtMs + 60_000;

test('all four captured feed surfaces normalize portfolio provenance and aliases', () => {
  for (const surface of INVO_FEED_SURFACES) {
    const row = normalizeFeedPortfolioObservation(fixture[surface], surface, capturedAtMs);
    assert.ok(row, surface);
    assert.equal(row.surface, surface);
    assert.equal(row.capturedAtMs, capturedAtMs);
    assert.equal(row.firstObservedAtMs, capturedAtMs);
    assert.equal(row.processedAtMs, capturedAtMs);
    assert.ok(row.epoch);
    assert.ok(row.sourceTradeAtMs && row.sourceTradeAtMs < capturedAtMs);
    assert.ok(row.rawPortfolioShapeKeys.length > 0);
  }
  const normiee = normalizeFeedPortfolioObservation(fixture.most_recent, 'most_recent', capturedAtMs);
  assert.ok(normiee);
  assert.equal(normiee.username, 'normiee');
  assert.equal(normiee.profile.plSnapshot, undefined, 'non-allowlisted payload is stripped');
  const withoutCanonicalReturn = classifyPortfolio({
    ...normiee.profile, percentChange: undefined,
  }, capturedAtMs, 'feed:most_recent');
  assert.ok(withoutCanonicalReturn);
  assert.equal(withoutCanonicalReturn.percentChange, null, 'plSnapshot is not guessed to be percentChange');
});

test('bounded evidence is restart-safe and deduplicates replayed feed posts', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'feed-evidence-')), 'evidence.json');
  const first = new FeedPortfolioEvidenceStore(path);
  assert.equal(first.observe([fixture.most_recent], 'most_recent', capturedAtMs).length, 1);
  assert.equal(first.observe([fixture.most_recent], 'most_recent', capturedAtMs).length, 0);
  const restarted = new FeedPortfolioEvidenceStore(path);
  assert.equal(restarted.report().uniquePortfolioCount, 1);
  assert.equal(restarted.observe([fixture.most_recent], 'most_recent', capturedAtMs).length, 0);
  assert.ok(existsSync(`${path}.journal.jsonl`));
  assert.equal(Object.keys(loadFeedPortfolioEvidence(path).portfolios).length, 1);
});

test('durable replay bloom survives observation rotation, compaction, restart, and selector reset', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'feed-replay-bloom-')), 'evidence.json');
  const store = new FeedPortfolioEvidenceStore(path);
  assert.equal(store.observe([fixture.most_recent], 'most_recent', capturedAtMs).length, 1);
  for (let i = 0; i < 3; i++) {
    const changed = structuredClone(fixture.most_recent);
    changed.id = `rotation-${i}`;
    changed.update.portfolio.percentChange += i + 1;
    store.observe([changed], 'most_recent', capturedAtMs + (i + 1) * 6 * 60_000);
  }
  writeFileSync(path, JSON.stringify({ version: 'obsolete', portfolios: {} }));
  const restarted = new FeedPortfolioEvidenceStore(path);
  assert.equal(restarted.observe([fixture.most_recent], 'most_recent', capturedAtMs + 24 * 60 * 60_000).length, 0);
});

test('feed-only profile without verified proof remains discovery evidence only', () => {
  const dir = mkdtempSync(join(tmpdir(), 'feed-selector-causal-'));
  const evidencePath = join(dir, 'feed.json');
  const statePath = join(dir, 'portfolio-candidates.json');
  const snapshotsPath = join(dir, 'candidate-snapshots.jsonl');
  new FeedPortfolioEvidenceStore(evidencePath).observe([fixture.most_recent], 'most_recent', capturedAtMs);
  const ledger = new PortfolioCandidateLedger(statePath, snapshotsPath);
  assert.equal(ledger.get('portfolio-normiee'), null);
  const result = ledger.assimilateFeedEvidence(
    Object.values(loadFeedPortfolioEvidence(evidencePath).portfolios), processedAtMs,
  );
  assert.equal(result.observationsProcessed, 1);
  assert.equal(result.totalFeedDiscoveredUniquePortfolios, 1);
  assert.equal(result.newVsBroadDiscovery, 1);
  assert.equal(result.newlySelectorQualified, 0);
  assert.equal(result.rejectedUnverified, 1);
  assert.equal(ledger.get('portfolio-normiee'), null);
});

test('canonical broad profile remains authoritative when feed evidence is assimilated', () => {
  const dir = mkdtempSync(join(tmpdir(), 'feed-selector-hydrated-'));
  const evidencePath = join(dir, 'feed.json');
  const ledger = new PortfolioCandidateLedger(join(dir, 'state.json'), join(dir, 'snapshots.jsonl'));
  new FeedPortfolioEvidenceStore(evidencePath).observe([fixture.most_recent], 'most_recent', capturedAtMs);
  ledger.observe([{ ...fixture.most_recent.update.portfolio, owner: { ...fixture.most_recent.update.owner, verified: true } }], 'trending', processedAtMs - 1);
  const result = ledger.assimilateFeedEvidence(Object.values(loadFeedPortfolioEvidence(evidencePath).portfolios), processedAtMs);
  assert.equal(result.newlySelectorQualified, 0, 'already-qualified hydrated profile is not relabelled new');
  assert.equal(ledger.get('portfolio-normiee')?.bucket, 'ELITE_CANDIDATE');
  assert.equal(ledger.get('portfolio-normiee')?.sourceFilter, 'trending', 'feed evidence must not overwrite canonical verified profile');
});

test('existing canonical ELITE verified=null is byte-for-byte unchanged by thin verified feed', () => {
  const dir = mkdtempSync(join(tmpdir(), 'feed-canonical-additive-'));
  const evidencePath = join(dir, 'feed.json');
  const ledger = new PortfolioCandidateLedger(join(dir, 'state.json'), join(dir, 'snapshots.jsonl'));
  ledger.observe([fixture.most_recent.update.portfolio], 'trending', processedAtMs - 1);
  const before = structuredClone(ledger.get('portfolio-normiee'));
  assert.equal(before?.bucket, 'ELITE_CANDIDATE');
  assert.equal(before?.verified, null);
  const thinVerified = structuredClone(fixture.most_recent);
  thinVerified.update.owner.verified = true;
  new FeedPortfolioEvidenceStore(evidencePath).observe([thinVerified], 'most_recent', processedAtMs);
  ledger.assimilateFeedEvidence(Object.values(loadFeedPortfolioEvidence(evidencePath).portfolios), processedAtMs);
  assert.deepEqual(ledger.get('portfolio-normiee'), before);
});

test('selector eligibility expires by immutable processing age', () => {
  const dir = mkdtempSync(join(tmpdir(), 'feed-selector-ttl-'));
  const evidencePath = join(dir, 'feed.json');
  new FeedPortfolioEvidenceStore(evidencePath).observe([fixture.most_recent], 'most_recent', capturedAtMs);
  const ledger = new PortfolioCandidateLedger(join(dir, 'state.json'), join(dir, 'snapshots.jsonl'));
  const result = ledger.assimilateFeedEvidence(
    Object.values(loadFeedPortfolioEvidence(evidencePath).portfolios),
    capturedAtMs + FEED_EVIDENCE_SELECTOR_TTL_MS + 1,
  );
  assert.equal(result.observationsProcessed, 0);
  assert.equal(ledger.get('portfolio-normiee'), null);
});

test('conflicting owner IDs and normalized usernames are rejected explicitly', () => {
  const idConflict = structuredClone(fixture.most_recent);
  idConflict.owner = { id: 'different-owner', username: 'normiee' };
  const nameConflict = structuredClone(fixture.most_recent);
  nameConflict.owner = { id: 'owner-normiee', username: 'OTHER' };
  assert.equal(normalizeFeedPortfolioObservation(idConflict, 'most_recent', capturedAtMs), null);
  assert.equal(normalizeFeedPortfolioObservation(nameConflict, 'most_recent', capturedAtMs), null);
});

test('malformed metric consistency cannot qualify', () => {
  const malformed = structuredClone(fixture.most_recent);
  malformed.update.owner.verified = true;
  malformed.update.portfolio.wonPositionsCount = 30;
  const path = join(mkdtempSync(join(tmpdir(), 'feed-malformed-')), 'feed.json');
  new FeedPortfolioEvidenceStore(path).observe([malformed], 'most_recent', capturedAtMs);
  const dir = mkdtempSync(join(tmpdir(), 'feed-malformed-ledger-'));
  const ledger = new PortfolioCandidateLedger(join(dir, 'state.json'), join(dir, 'snapshots.jsonl'));
  const result = ledger.assimilateFeedEvidence(Object.values(loadFeedPortfolioEvidence(path).portfolios), processedAtMs);
  assert.notEqual(ledger.get('portfolio-normiee')?.bucket, 'ELITE_CANDIDATE');
  assert.equal(result.rejectedMalformed, 1);
});

test('conflicting metric aliases fail closed instead of selecting a favorable value', () => {
  const raw = { ...fixture.most_recent.update.portfolio, owner: { ...fixture.most_recent.update.owner, verified: true },
    closedPositions: 999, closedPositionsCount: 25, winRate: 99, winRatePct: 84,
    percentChange: 9999, pnlPercent: 250 };
  const row = classifyPortfolio(raw, processedAtMs, 'trending');
  assert.ok(row);
  assert.notEqual(row.bucket, 'ELITE_CANDIDATE');
  assert.ok(row.reasons.includes('inconsistent_profile_metrics') || row.reasons.includes('incomplete_profile_metrics'));
});

test('feed assimilation cannot move canonical cycle freshness or displace active incumbent at capacity', () => {
  const dir = mkdtempSync(join(tmpdir(), 'feed-direct-race-'));
  const statePath = join(dir, 'candidates.json');
  const snapshotsPath = join(dir, 'snapshots.jsonl');
  const evidencePath = join(dir, 'feed.json');
  const ledger = new PortfolioCandidateLedger(statePath, snapshotsPath);
  ledger.observe([{ ...fixture.most_recent.update.portfolio, id: 'incumbent', owner: { id: 'owner-inc', username: 'inc', verified: true } }], 'trending', processedAtMs);
  const before = readFileSync(statePath, 'utf8');
  new FeedPortfolioEvidenceStore(evidencePath).observe([fixture.most_recent], 'most_recent', processedAtMs + 1);
  ledger.assimilateFeedEvidence(Object.values(loadFeedPortfolioEvidence(evidencePath).portfolios), processedAtMs);
  const after = JSON.parse(readFileSync(statePath, 'utf8'));
  assert.equal(after.lastObservedAtMs, processedAtMs);
  assert.deepEqual(after.portfolios.incumbent, JSON.parse(before).portfolios.incumbent);
  const loaded = loadEliteDirectTargets(statePath, processedAtMs + 2, 20 * 60_000);
  const watch = new EliteDirectWatchState(join(dir, 'watch.json'));
  const incumbent = loaded.targets.find(row => row.portfolioId === 'incumbent')!;
  watch.syncTargets([incumbent], new Set(), processedAtMs, true, 120_000, new Set(), 1);
  watch.commitClosedHydration('incumbent', [], processedAtMs);
  watch.commitOpenBaseline('incumbent', [], processedAtMs);
  watch.syncTargets(loaded.targets, new Set(), processedAtMs + 2, true, 120_000, new Set(loaded.demotedPortfolioIds), 1);
  assert.equal(watch.status().activeTargetCount, 1);
  assert.ok(JSON.parse(readFileSync(join(dir, 'watch.json'), 'utf8')).targets.incumbent);
});

test('rotating evidence stays within portfolio, state-byte and journal-byte caps', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'feed-cap-')), 'feed.json');
  const store = new FeedPortfolioEvidenceStore(path);
  const posts = Array.from({ length: FEED_EVIDENCE_MAX_PORTFOLIOS + 25 }, (_, index) => ({ id: `post-${index}`, update: { portfolio: { ...fixture.most_recent.update.portfolio, id: `portfolio-${index}` }, owner: { id: `owner-${index}`, username: `user-${index}` } } }));
  store.observe(posts, 'most_recent', capturedAtMs);
  assert.ok(store.report().uniquePortfolioCount <= FEED_EVIDENCE_MAX_PORTFOLIOS);
  assert.ok(statSync(path).size <= FEED_EVIDENCE_MAX_STATE_BYTES);
  assert.ok(statSync(`${path}.journal.jsonl`).size <= FEED_EVIDENCE_MAX_JOURNAL_BYTES);
});

test('100-post hot batch uses bounded small journal records and at most one full rewrite', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'feed-amplification-')), 'feed.json');
  const store = new FeedPortfolioEvidenceStore(path);
  const posts = Array.from({ length: 100 }, (_, index) => ({ id: `batch-${index}`, update: { portfolio: { ...fixture.most_recent.update.portfolio, id: `batch-portfolio-${index}` }, owner: { id: `batch-owner-${index}`, username: `batch-user-${index}` } } }));
  store.observe(posts, 'most_recent', capturedAtMs);
  const report = store.report();
  assert.ok(report.evidenceBytes <= FEED_EVIDENCE_MAX_STATE_BYTES);
  assert.ok(report.journalBytes <= FEED_EVIDENCE_MAX_JOURNAL_BYTES);
  assert.ok(report.fullRewriteCount <= 1);
  assert.ok(report.evidenceBytes < 1_000_000, '100 posts must not manufacture a megabyte replay bitmap');
});

test('historical feed trade cannot become copy-eligible after portfolio assimilation', () => {
  const dir = mkdtempSync(join(tmpdir(), 'feed-selector-no-replay-'));
  const evidencePath = join(dir, 'feed.json');
  const statePath = join(dir, 'portfolio-candidates.json');
  const snapshotsPath = join(dir, 'candidate-snapshots.jsonl');
  new FeedPortfolioEvidenceStore(evidencePath).observe([fixture.most_recent], 'most_recent', capturedAtMs);
  const ledger = new PortfolioCandidateLedger(statePath, snapshotsPath);
  ledger.observe([{ ...fixture.most_recent.update.portfolio, owner: { ...fixture.most_recent.update.owner, verified: true } }], 'trending', processedAtMs);
  ledger.assimilateFeedEvidence(Object.values(loadFeedPortfolioEvidence(evidencePath).portfolios), processedAtMs);
  const historicalDecision = eliteAdmissionFromState(
    statePath, 'portfolio-normiee', capturedAtMs, 20 * 60_000, snapshotsPath,
  );
  assert.equal(historicalDecision.allowed, false);
  assert.equal(historicalDecision.reason, 'candidate_state_from_future');
  // Even a later decision is still blocked until #404's direct-watch admission index admits it.
  const futureDecision = eliteAdmissionFromState(
    statePath, 'portfolio-normiee', processedAtMs + 1, 20 * 60_000, snapshotsPath,
  );
  assert.equal(futureDecision.allowed, false);
  assert.equal(futureDecision.reason, 'direct_watch_admission_index_missing');
});

test('research ingestion is durable and does not reprocess evidence after restart', () => {
  const dir = mkdtempSync(join(tmpdir(), 'feed-selector-restart-'));
  const evidencePath = join(dir, 'feed.json');
  const statePath = join(dir, 'portfolio-candidates.json');
  const snapshotsPath = join(dir, 'candidate-snapshots.jsonl');
  new FeedPortfolioEvidenceStore(evidencePath).observe([fixture.most_recent], 'most_recent', capturedAtMs);
  const records = Object.values(loadFeedPortfolioEvidence(evidencePath).portfolios);
  new PortfolioCandidateLedger(statePath, snapshotsPath).assimilateFeedEvidence(records, processedAtMs);
  const restarted = new PortfolioCandidateLedger(statePath, snapshotsPath);
  assert.equal(restarted.assimilateFeedEvidence(records, processedAtMs + 60_000).observationsProcessed, 0);
  assert.equal(existsSync(snapshotsPath), false, 'unverified evidence never writes a candidate snapshot');
});

test('selector-version mismatch quarantines retained evidence instead of re-stamping it now', () => {
  const dir = mkdtempSync(join(tmpdir(), 'feed-selector-version-'));
  const evidencePath = join(dir, 'feed.json');
  const statePath = join(dir, 'portfolio-candidates.json');
  new FeedPortfolioEvidenceStore(evidencePath).observe([fixture.most_recent], 'most_recent', capturedAtMs);
  writeFileSync(statePath, JSON.stringify({ version: 1, selectorVersion: 'obsolete-selector', portfolios: {}, firstEliteAtMs: {}, lastObservedAtMs: 0 }));
  const ledger = new PortfolioCandidateLedger(statePath, join(dir, 'snapshots.jsonl'));
  const result = ledger.assimilateFeedEvidence(Object.values(loadFeedPortfolioEvidence(evidencePath).portfolios), processedAtMs);
  assert.equal(result.observationsProcessed, 0);
  assert.equal(ledger.get('portfolio-normiee'), null);
});
