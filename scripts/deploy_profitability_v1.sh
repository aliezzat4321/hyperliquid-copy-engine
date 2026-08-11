#!/usr/bin/env bash
set -euo pipefail

cd /root/hyperliquid-copy-engine

echo '[1/7] Updating main'
git fetch origin main
git checkout main
git merge --ff-only origin/main

echo '[2/7] Installing package'
.venv/bin/pip install -e '.[dev]'

echo '[3/7] Running profitability regression tests'
.venv/bin/python -m pytest -q tests/test_live_profitability_v1.py

echo '[4/7] Installing systemd units'
install -m 0644 deploy/systemd/hyperliquid-profitability.service /etc/systemd/system/hyperliquid-profitability.service
install -m 0644 deploy/systemd/hyperliquid-profitability.timer /etc/systemd/system/hyperliquid-profitability.timer
systemctl daemon-reload

echo '[5/7] Enabling profitability timer'
systemctl enable --now hyperliquid-profitability.timer

echo '[6/7] Running scorer immediately'
systemctl start hyperliquid-profitability.service

echo '[7/7] Verifying output and safety'
systemctl --no-pager --full status hyperliquid-profitability.timer || true
systemctl --no-pager --full status hyperliquid-profitability.service || true

test -f /mnt/HC_Volume_106576526/hyperliquid/profitability/master_profitability.json

grep -q 'REAL_TRADING_ENABLED=NO' deploy/systemd/hyperliquid-profitability.service

echo
printf 'Deployed commit: '
git rev-parse HEAD
printf 'Master leaderboard: '
ls -lh /mnt/HC_Volume_106576526/hyperliquid/profitability/master_profitability.json
if test -f /mnt/HC_Volume_106576526/hyperliquid/profitability/ranked_profitability.json; then
  printf 'Ranked leaderboard: '
  ls -lh /mnt/HC_Volume_106576526/hyperliquid/profitability/ranked_profitability.json
fi

echo 'Profitability deployment complete; real trading remains disabled.'
