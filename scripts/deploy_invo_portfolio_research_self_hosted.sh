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

# Run immediately once; the timer then keeps it current.
systemctl start "$SERVICE_UNIT"
systemctl restart "$TIMER_UNIT"

candidate="$STATE/portfolio-candidates.json"
elite="$STATE/elite-shadow-report.json"
if [[ ! -s "$candidate" ]]; then
  echo "candidate ledger missing after research cycle" >&2
  journalctl -u "$SERVICE_UNIT" -n 120 --no-pager || true
  exit 1
fi
if [[ ! -s "$elite" ]]; then
  echo "elite shadow report missing after research cycle" >&2
  journalctl -u "$SERVICE_UNIT" -n 120 --no-pager || true
  exit 1
fi

node - "$candidate" "$elite" <<'NODE'
const fs = require('fs');
const [candidatePath, elitePath] = process.argv.slice(2);
const candidate = JSON.parse(fs.readFileSync(candidatePath, 'utf8'));
const elite = JSON.parse(fs.readFileSync(elitePath, 'utf8'));
const portfolios = Object.values(candidate.portfolios || {});
if (!portfolios.length) throw new Error('Invo portfolio discovery returned zero portfolios');
if (candidate.selectorVersion !== 'invo-portfolio-elite-v1-20260916') throw new Error(`unexpected selector version ${candidate.selectorVersion}`);
if (elite.liveTrading !== false) throw new Error('elite shadow report must prove live trading false');
console.log(JSON.stringify({
  PORTFOLIO_RESEARCH_DEPLOYED: true,
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
