#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

REPO="aliezzat4321/hyperliquid-copy-engine"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPT=/opt/hyperliquid-ai-team
STATE=/var/lib/hyperliquid-ai-team-supervisor

[[ -f "$ROOT/AGENTS.md" ]] || { echo "not a Hyperliquid repo checkout" >&2; exit 1; }
git -C "$ROOT" remote get-url origin | grep -Eq 'github\.com[:/]aliezzat4321/hyperliquid-copy-engine(\.git)?$' || {
  echo "repository safety mismatch" >&2
  exit 1
}

echo 'TARGET_PROJECT=HYPERLIQUID_ONLY'
echo 'POLYMARKET_INSPECTION=NO'
echo 'POLYMARKET_MUTATION=NO'
echo 'REAL_TRADING_CHANGE=NO'

install -d -o root -g root -m 0700 "$STATE"
install -d -o root -g root -m 0755 "$OPT/scripts"
install -o root -g root -m 0755 \
  "$ROOT/scripts/ai_team_external_supervisor.py" \
  "$OPT/scripts/ai_team_external_supervisor.py"
install -o root -g root -m 0644 \
  "$ROOT/deploy/systemd/hyperliquid-ai-team-supervisor.service" \
  /etc/systemd/system/hyperliquid-ai-team-supervisor.service
install -o root -g root -m 0644 \
  "$ROOT/deploy/systemd/hyperliquid-ai-team-supervisor.timer" \
  /etc/systemd/system/hyperliquid-ai-team-supervisor.timer

python3 -m py_compile "$OPT/scripts/ai_team_external_supervisor.py"
systemctl daemon-reload
systemctl enable --now hyperliquid-ai-team-supervisor.timer >/dev/null

echo 'AI_TEAM_EXTERNAL_SUPERVISOR_INSTALL=OK'
echo 'SUPERVISOR_STATE_ROOT=/var/lib/hyperliquid-ai-team-supervisor'
echo 'SUPERVISOR_TIMER_ENABLED_AND_ACTIVE=YES'
echo 'MODEL_CALLS=NONE'
echo 'POLYMARKET_TOUCHED=NO'
echo 'REAL_TRADING_TOUCHED=NO'
