import { resolve } from 'path';
import { existsSync } from 'fs';
import { INVO_REFRESH_TOKEN, INVO_TOKEN, validateEnv } from './env.js';
import * as invo from './invo-client.js';
import { PortfolioCandidateLedger } from './portfolio-candidates.js';
import { loadFeedPortfolioEvidence } from './feed-portfolio-evidence.js';
import {
  CANONICAL_INVO_HORIZONS,
  InvoLeaderboardLedger,
  apiFilterForLeaderboardSurface,
  normalizeLeaderboardHorizon,
  type InvoLeaderboardHorizon,
  type InvoLeaderboardSurface,
} from './invo-leaderboards.js';

if (INVO_TOKEN) invo.setToken(INVO_TOKEN);
if (INVO_REFRESH_TOKEN) invo.setRefreshToken(INVO_REFRESH_TOKEN);

function n(name: string, fallback: number): number {
  const raw = process.env[name];
  const value = raw == null || raw === '' ? fallback : Number(raw);
  if (!Number.isFinite(value)) throw new Error(`Invalid ${name}: ${raw}`);
  return value;
}

function discoveryFilters() {
  const raw = process.env.INVO_PORTFOLIO_DISCOVERY_FILTERS ?? 'trending,all';
  return [...new Set(raw.split(',').map(v => v.trim().toLowerCase()).filter(Boolean))];
}

function leaderboardSurfaces(): InvoLeaderboardSurface[] {
  const raw = process.env.INVO_PORTFOLIO_LEADERBOARD_HORIZONS ?? CANONICAL_INVO_HORIZONS.join(',');
  const horizons = raw.split(',').map(normalizeLeaderboardHorizon).filter((v): v is InvoLeaderboardHorizon => Boolean(v));
  const uniqueHorizons = [...new Set(horizons)];
  if (!uniqueHorizons.length) throw new Error('INVO_PORTFOLIO_LEADERBOARD_HORIZONS resolved to no supported horizons');
  return ['CROWN', ...uniqueHorizons];
}

async function main() {
  validateEnv(false);
  await invo.ensureToken();

  const statePath = resolve(process.env.INVO_PORTFOLIO_CANDIDATE_STATE_PATH ?? 'data/portfolio-candidates.json');
  const snapshotsPath = resolve(process.env.INVO_PORTFOLIO_CANDIDATE_SNAPSHOTS_PATH ?? 'data/portfolio-candidate-snapshots.jsonl');
  const leaderboardStatePath = resolve(process.env.INVO_PORTFOLIO_LEADERBOARD_STATE_PATH ?? 'data/invo-leaderboards.json');
  const leaderboardSnapshotsPath = resolve(process.env.INVO_PORTFOLIO_LEADERBOARD_SNAPSHOTS_PATH ?? 'data/invo-leaderboard-snapshots.jsonl');
  const feedEvidencePath = resolve(process.env.INVO_FEED_PORTFOLIO_EVIDENCE_PATH
    ?? '/var/lib/hyperliquid-copy-engine/invo-notification-executor/feed-portfolio-evidence.json');
  const pages = Math.max(1, Math.min(20, Math.trunc(n('INVO_PORTFOLIO_DISCOVERY_PAGES', 4))));
  const pageSize = Math.max(1, Math.min(100, Math.trunc(n('INVO_PORTFOLIO_DISCOVERY_PAGE_SIZE', 50))));
  const leaderboardPages = Math.max(1, Math.min(20, Math.trunc(n('INVO_PORTFOLIO_LEADERBOARD_PAGES', pages))));
  const leaderboardPageSize = Math.max(1, Math.min(100, Math.trunc(n('INVO_PORTFOLIO_LEADERBOARD_PAGE_SIZE', pageSize))));
  const ledger = new PortfolioCandidateLedger(statePath, snapshotsPath);
  const leaderboardLedger = new InvoLeaderboardLedger(leaderboardStatePath, leaderboardSnapshotsPath);
  const observedAtMs = Date.now();

  // Authenticated runtime probing proved the default crown Top 10 is filter=trending,
  // while the explicit UI horizons are filter=1D/1W/1M/1Y/AT. Persist all six
  // as independent ranked surfaces so crown rank history cannot disappear into broad research.
  const leaderboardResults: Array<Record<string, unknown>> = [];
  for (const surface of leaderboardSurfaces()) {
    const sourceFilter = apiFilterForLeaderboardSurface(surface);
    let accepted = 0;
    let failure: string | null = null;
    for (let page = 1; page <= leaderboardPages; page++) {
      try {
        const data = await invo.discoverPortfolios(sourceFilter, page, leaderboardPageSize);
        const items = Array.isArray(data?.items) ? data.items : [];
        if (!items.length) break;
        accepted += leaderboardLedger.observePage(items, surface, observedAtMs, (page - 1) * leaderboardPageSize).length;
        if (items.length < leaderboardPageSize) break;
      } catch (err) {
        failure = err instanceof Error ? err.message : String(err);
        break;
      }
    }
    leaderboardResults.push({ surface, sourceFilter, accepted, failure });
  }

  // Broad discovery remains separate from ranked leaderboard evidence and from elite-shadow PnL.
  const endpointResults: Array<Record<string, unknown>> = [];
  let rawItems = 0;
  for (const filter of discoveryFilters()) {
    let accepted = 0;
    let failure: string | null = null;
    for (let page = 1; page <= pages; page++) {
      try {
        const data = await invo.discoverPortfolios(filter, page, pageSize);
        const items = Array.isArray(data?.items) ? data.items : [];
        if (!items.length) break;
        rawItems += items.length;
        const observed = ledger.observe(items, filter, observedAtMs);
        accepted += observed.length;
        if (items.length < pageSize) break;
      } catch (err) {
        failure = err instanceof Error ? err.message : String(err);
        break;
      }
    }
    endpointResults.push({ filter, accepted, failure });
  }
  // Feed evidence is ingested only here, after the broad endpoint cycle. Its source
  // trade time is provenance; observedAtMs is this research processing boundary.
  const assimilationSuspensionPath = `${feedEvidencePath}.assimilation-suspended.json`;
  const feedEvidence = loadFeedPortfolioEvidence(feedEvidencePath);
  const feedAssimilation = existsSync(assimilationSuspensionPath)
    ? { ...ledger.feedExpansionReport(observedAtMs), observationsProcessed: 0,
      assimilationSuspended: true, suspensionMarkerPath: assimilationSuspensionPath }
    : { ...ledger.assimilateFeedEvidence(Object.values(feedEvidence.portfolios), observedAtMs),
      assimilationSuspended: false };
  const report = ledger.report();
  const leaderboards = leaderboardLedger.report();
  console.log(JSON.stringify({
    ok: true,
    observedAtMs,
    sourceEndpoint: '/v1_0/trending/get_portfolios_pl',
    leaderboardResults,
    leaderboards: {
      leaderboardVersion: leaderboards.leaderboardVersion,
      surfaces: leaderboards.surfaces,
      horizons: leaderboards.horizons,
      countsBySurface: leaderboards.countsBySurface,
      countsByHorizon: leaderboards.countsByHorizon,
      uniquePortfolioCount: leaderboards.uniquePortfolioCount,
      uniqueOwnerCount: leaderboards.uniqueOwnerCount,
      top10BySurface: Object.fromEntries(leaderboards.surfaces.map(surface => [
        surface,
        leaderboards.latestBySurface[surface].slice(0, 10).map(row => ({
          rank: row.rank,
          portfolioId: row.portfolioId,
          portfolioName: row.portfolioName,
          username: row.username,
          percentChange: row.percentChange,
          closedPositions: row.closedPositions,
          winRatePct: row.winRatePct,
          sourceFilter: row.sourceFilter,
        })),
      ])),
      top10ByHorizon: Object.fromEntries(leaderboards.horizons.map(horizon => [
        horizon,
        leaderboards.latestByHorizon[horizon].slice(0, 10).map(row => ({
          rank: row.rank,
          portfolioId: row.portfolioId,
          portfolioName: row.portfolioName,
          username: row.username,
          percentChange: row.percentChange,
          closedPositions: row.closedPositions,
          winRatePct: row.winRatePct,
        })),
      ])),
      crossSurfaceTop: leaderboards.crossSurface.slice(0, 50),
      crossHorizonTop: leaderboards.crossHorizon.slice(0, 50),
    },
    broadDiscovery: {
      rawItems,
      endpointResults,
      selectorVersion: report.selectorVersion,
      policy: report.policy,
      uniquePortfolioCount: report.uniquePortfolioCount,
      uniqueOwnerCount: report.uniqueOwnerCount,
      buckets: report.buckets,
      elite: report.portfolios.filter(p => p.bucket === 'ELITE_CANDIDATE').slice(0, 50).map(p => ({
        portfolioId: p.portfolioId,
        portfolioName: p.portfolioName,
        username: p.username,
        closedPositions: p.closedPositions,
        winRatePct: p.winRatePct,
        percentChange: p.percentChange,
        winLossRatio: p.winLossRatio,
        score: p.score,
        sourceFilter: p.sourceFilter,
      })),
    },
    feedCandidateExpansion: {
      evidenceVersion: feedEvidence.version,
      evidenceGeneratedAtMs: feedEvidence.generatedAtMs,
      ...feedAssimilation,
    },
    leaderboardSelectionPolicy: 'COLLECT_FIRST_REVIEW_BEFORE_ELITE_SELECTOR_CHANGE',
    retroactiveSelectionForbidden: true,
    liveTrading: false,
  }, null, 2));
}

main().catch(err => {
  console.error(JSON.stringify({ ok: false, error: err instanceof Error ? err.message : String(err) }));
  process.exit(1);
});
