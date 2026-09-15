import { resolve } from 'path';
import { INVO_REFRESH_TOKEN, INVO_TOKEN, validateEnv } from './env.js';
import * as invo from './invo-client.js';
import { PortfolioCandidateLedger } from './portfolio-candidates.js';

if (INVO_TOKEN) invo.setToken(INVO_TOKEN);
if (INVO_REFRESH_TOKEN) invo.setRefreshToken(INVO_REFRESH_TOKEN);

function n(name: string, fallback: number): number {
  const raw = process.env[name];
  const value = raw == null || raw === '' ? fallback : Number(raw);
  if (!Number.isFinite(value)) throw new Error(`Invalid ${name}: ${raw}`);
  return value;
}

function filters() {
  const raw = process.env.INVO_PORTFOLIO_DISCOVERY_FILTERS ?? 'trending,all';
  return [...new Set(raw.split(',').map(v => v.trim().toLowerCase()).filter(Boolean))];
}

async function main() {
  validateEnv(false);
  await invo.ensureToken();

  const statePath = resolve(process.env.INVO_PORTFOLIO_CANDIDATE_STATE_PATH ?? 'data/portfolio-candidates.json');
  const snapshotsPath = resolve(process.env.INVO_PORTFOLIO_CANDIDATE_SNAPSHOTS_PATH ?? 'data/portfolio-candidate-snapshots.jsonl');
  const pages = Math.max(1, Math.min(20, Math.trunc(n('INVO_PORTFOLIO_DISCOVERY_PAGES', 4))));
  const pageSize = Math.max(1, Math.min(100, Math.trunc(n('INVO_PORTFOLIO_DISCOVERY_PAGE_SIZE', 50))));
  const ledger = new PortfolioCandidateLedger(statePath, snapshotsPath);
  const observedAtMs = Date.now();

  const endpointResults: Array<Record<string, unknown>> = [];
  let rawItems = 0;
  for (const filter of filters()) {
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
  console.log(JSON.stringify({
    ok: true,
    observedAtMs,
    sourceEndpoint: '/v1_0/trending/get_portfolios_pl',
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
  }, null, 2));
}

main().catch(err => {
  console.error(JSON.stringify({ ok: false, error: err instanceof Error ? err.message : String(err) }));
  process.exit(1);
});
