#!/usr/bin/env bash
set -euo pipefail

REPO=/root/hyperliquid-copy-engine
SERVICE_REL=services/invo-notification-executor
SERVICE_DIR="$REPO/$SERVICE_REL"
STATE=/var/lib/hyperliquid-copy-engine/invo-notification-executor
INVO_ENV=/etc/hyperliquid-copy-engine/invo.env
SERVICE_UNIT=hyperliquid-invo-portfolio-research.service
TIMER_UNIT=hyperliquid-invo-portfolio-research.timer

if [[ "$(id -u)" -ne 0 ]]; then
  echo "portfolio research deployment requires root" >&2
  exit 2
fi
if [[ ! -d "$REPO/.git" ]]; then
  echo "missing canonical repository: $REPO" >&2
  exit 2
fi
if [[ ! -s "$INVO_ENV" ]]; then
  echo "missing Invo credential file: $INVO_ENV" >&2
  exit 2
fi
if ! grep -Eq '^(INVO_ACCESS_TOKEN|INVO_REFRESH_TOKEN)=' "$INVO_ENV"; then
  echo "$INVO_ENV does not contain Invo credentials" >&2
  exit 2
fi

cd "$REPO"
git fetch origin main
git checkout main
git clean -fdx -- "$SERVICE_REL/package-lock.json" "$SERVICE_REL/node_modules" "$SERVICE_REL/dist"
git merge --ff-only origin/main

install -d -m 0700 "$STATE"
cd "$SERVICE_DIR"
npm install --ignore-scripts --no-audit --no-fund
npm run check

install -m 0644 "$REPO/deploy/systemd/$SERVICE_UNIT" "/etc/systemd/system/$SERVICE_UNIT"
install -m 0644 "$REPO/deploy/systemd/$TIMER_UNIT" "/etc/systemd/system/$TIMER_UNIT"
systemctl daemon-reload
systemd-analyze verify "/etc/systemd/system/$SERVICE_UNIT" "/etc/systemd/system/$TIMER_UNIT"
systemctl enable "$TIMER_UNIT"

# Run immediately once; the timer then keeps broad discovery and all ranked Invo leaderboard surfaces current.
systemctl start "$SERVICE_UNIT"
systemctl restart "$TIMER_UNIT"

candidate="$STATE/portfolio-candidates.json"
leaderboards="$STATE/invo-leaderboards.json"
leaderboard_snapshots="$STATE/invo-leaderboard-snapshots.jsonl"
elite="$STATE/elite-shadow-report.json"
for required in "$candidate" "$leaderboards" "$leaderboard_snapshots" "$elite"; do
  if [[ ! -s "$required" ]]; then
    echo "required portfolio research artifact missing: $required" >&2
    journalctl -u "$SERVICE_UNIT" -n 160 --no-pager || true
    exit 1
  fi
done

node - "$candidate" "$leaderboards" "$elite" "$SERVICE_DIR/src/portfolio-candidates.ts" <<'NODE'
const fs = require('fs');
const [candidatePath, leaderboardPath, elitePath, selectorSourcePath] = process.argv.slice(2);
const candidate = JSON.parse(fs.readFileSync(candidatePath, 'utf8'));
const leaderboards = JSON.parse(fs.readFileSync(leaderboardPath, 'utf8'));
const elite = JSON.parse(fs.readFileSync(elitePath, 'utf8'));
const selectorSource = fs.readFileSync(selectorSourcePath, 'utf8');
const selectorMatch = selectorSource.match(
  /ELITE_SELECTOR_VERSION\s*=\s*['"]([^'"]+)['"]/,
);
if (!selectorMatch) throw new Error('unable to resolve selector version from source');
const expectedSelector = selectorMatch[1];
const portfolios = Object.values(candidate.portfolios || {});
if (!portfolios.length) throw new Error('Invo portfolio discovery returned zero portfolios');
if (candidate.selectorVersion !== expectedSelector) {
  throw new Error(
    `unexpected selector version ${candidate.selectorVersion}; expected ${expectedSelector}`,
  );
}
if (leaderboards.leaderboardVersion !== 'invo-top-portfolios-surfaces-v2-20260916') throw new Error(`unexpected leaderboard version ${leaderboards.leaderboardVersion}`);
const requiredSurfaces = ['CROWN', '1D', '1W', '1M', '1Y', 'AT'];
const countsBySurface = {};
for (const surface of requiredSurfaces) {
  const rows = leaderboards.latestBySurface?.[surface];
  if (!Array.isArray(rows) || rows.length === 0) throw new Error(`missing/non-populated Invo leaderboard surface ${surface}`);
  const expectedFilter = surface === 'CROWN' ? 'trending' : surface;
  if (!rows.every((row, index) => row.surface === surface && row.sourceFilter === expectedFilter && row.rank === index + 1 && row.sourceEndpoint === '/v1_0/trending/get_portfolios_pl')) {
    throw new Error(`invalid rank/surface/filter provenance for ${surface}`);
  }
  countsBySurface[surface] = rows.length;
}
if (elite.liveTrading !== false) throw new Error('elite shadow report must prove live trading false');
console.log(JSON.stringify({
  PORTFOLIO_RESEARCH_DEPLOYED: true,
  EXACT_INVO_LEADERBOARDS: true,
  CROWN_INVO_LEADERBOARD: true,
  leaderboardVersion: leaderboards.leaderboardVersion,
  leaderboardEndpoint: leaderboards.sourceEndpoint,
  leaderboardCountsBySurface: countsBySurface,
  leaderboardUniquePortfolios: new Set(requiredSurfaces.flatMap(s => leaderboards.latestBySurface[s].map(row => row.portfolioId))).size,
  selectorVersion: candidate.selectorVersion,
  discoveredPortfolios: portfolios.length,
  uniqueOwners: new Set(portfolios.map(p => p.ownerId).filter(Boolean)).size,
  eliteCandidates: portfolios.filter(p => p.bucket === 'ELITE_CANDIDATE').length,
  sparseHighReturn: portfolios.filter(p => p.bucket === 'SPARSE_HIGH_RETURN').length,
  researchWide: portfolios.filter(p => p.bucket === 'RESEARCH_WIDE').length,
  rejected: portfolios.filter(p => p.bucket === 'REJECTED_DEMOTED').length,
  eliteShadowOpenPositions: elite.openElitePositions,
  eliteShadowObservedNetPnlUsd: elite.totalObservedNetPnlUsd,
  retroactiveSelectionForbidden: elite.retroactiveSelectionForbidden,
  liveTrading: elite.liveTrading,
}, null, 2));
NODE

systemctl --no-pager --full status "$TIMER_UNIT" | head -30 || true
systemctl --no-pager --full status "$SERVICE_UNIT" | head -30 || true
