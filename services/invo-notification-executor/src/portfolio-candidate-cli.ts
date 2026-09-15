import { resolve } from 'path';
import { INVO_REFRESH_TOKEN, INVO_TOKEN, validateEnv } from './env.js';
import * as invo from './invo-client.js';
import { PortfolioCandidateLedger } from './portfolio-candidates.js';
import {
  CANONICAL_INVO_HORIZONS,
  InvoLeaderboardLedger,
  normalizeLeaderboardHorizon,
  type InvoLeaderboardHorizon,
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

function leaderboardHorizons(): InvoLeaderboardHorizon[] {
  const raw = process.env.INVO_PORTFOLIO_LEADERBOARD_HORIZONS ?? CANONICAL_INVO_HORIZONS.join(',');
  const horizons = raw.split(',').map(normalizeLeaderboardHorizon).filter((v): v is InvoLeaderboardHorizon => Boolean(v));
  const unique = [...new Set(horizons)];
  if (!unique.length) throw new Error('INVO_PORTFOLIO_LEADERBOARD_HORIZONS resolved to no supported horizons');
  return unique;
}

async function main() {
  validateEnv(false);
  await invo.ensureToken();

  const statePath = resolve(process.env.INVO_PORTFOLIO_CANDIDATE_STATE_PATH ?? 'data/portfolio-candidates.json');
  const snapshotsPath = resolve(process.env.INVO_PORTFOLIO_CANDIDATE_SNAPSHOTS_PATH ?? 'data/portfolio-candidate-snapshots.jsonl');
  const leaderboardStatePath = resolve(process.env.INVO_PORTFOLIO_LEADERBOARD_STATE_PATH ?? 'data/invo-leaderboards.json');
  const leaderboardSnapshotsPath = resolve(process.env.INVO_PORTFOLIO_LEADERBOARD_SNAPSHOTS_PATH ?? 'data/invo-leaderboard-snapshots.jsonl');
  const pages = Math.max(1, Math.min(20, Math.trunc(n('INVO_PORTFOLIO_DISCOVERY_PAGES', 4))));
  const pageSize = Math.max(1, Math.min(100, Math.trunc(n('INVO_PORTFOLIO_DISCOVERY_PAGE_SIZE', 50))));
  const leaderboardPages = Math.max(1, Math.min(20, Math.trunc(n('INVO_PORTFOLIO_LEADERBOARD_PAGES', pages))));
  const leaderboardPageSize = Math.max(1, Math.min(100, Math.trunc(n('INVO_PORTFOLIO_LEADERBOARD_PAGE_SIZE', pageSize))));
  const ledger = new PortfolioCandidateLedger(statePath, snapshotsPath);
  const leaderboardLedger = new InvoLeaderboardLedger(leaderboardStatePath, leaderboardSnapshotsPath);
  const observedAtMs = Date.now();

  // Exact Invo UI Top Portfolios surfaces. Runtime probing on 2026-09-15 proved
  // the horizon is the `filter` enum itself: 1D, 1W, 1M, 1Y, AT.
  const leaderboardResults: Array<Record<string, unknown>> = [];
  for (const horizon of leaderboardHorizons()) {
    let accepted = 0;
    let failure: string | null = null;
    for (let page = 1; page <= leaderboardPages; page++) {
      try {
        const data = await invo.discoverPortfolios(horizon, page, leaderboardPageSize);
        const items = Array.isArray(data?.items) ? data.items : [];
        if (!items.length) break;
        accepted += leaderboardLedger.observePage(items, horizon, observedAtMs, (page - 1) * leaderboardPageSize).length;
        if (items.length < leaderboardPageSize) break;
      } catch (err) {
        failure = err instanceof Error ? err.message : String(err);
        break;
      }
    }
    leaderboardResults.push({ horizon, accepted, failure });
  }

  // Broad discovery remains separate from leaderboard rank evidence and from elite-shadow PnL.
  // This preserves the existing prospective selector while the new cross-horizon methodology is reviewed.
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
        accepted += ledger.observe(items, filter, observedAtMs).length;
        if (items.length < pageSize) break;
      } catch (err) {
        failure = err instanceof Error ? err.message : String(err);
        break;
      }
    }
    endpointResults.push({ filter, accepted, failure });
  }

  const report = ledger.report();
  const leaderboards = leaderboardLedger.report();
  console.log(JSON.stringify({
    ok: true,
    observedAtMs,
    sourceEndpoint: '/v1_0/trending/get_portfolios_pl',
    leaderboardResults,
    leaderboards: {
      leaderboardVersion: leaderboards.leaderboardVersion,
      horizons: leaderboards.horizons,
      countsByHorizon: leaderboards.countsByHorizon,
      uniquePortfolioCount: leaderboards.uniquePortfolioCount,
      uniqueOwnerCount: leaderboards.uniqueOwnerCount,
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
    leaderboardSelectionPolicy: 'COLLECT_FIRST_REVIEW_BEFORE_ELITE_SELECTOR_CHANGE',
    retroactiveSelectionForbidden: true,
    liveTrading: false,
  }, null, 2));
}

main().catch(err => {
  console.error(JSON.stringify({ ok: false, error: err instanceof Error ? err.message : String(err) }));
  process.exit(1);
});
