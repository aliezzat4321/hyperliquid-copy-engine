#!/usr/bin/env bash
set -euo pipefail

REPO=/root/hyperliquid-copy-engine
SERVICE_DIR="$REPO/services/invo-notification-executor"
UNIT=hyperliquid-invo-notification-executor.service
STATE=/var/lib/hyperliquid-copy-engine/invo-notification-executor
INVO_ENV=/etc/hyperliquid-copy-engine/invo.env
EXEC_ENV=/etc/hyperliquid-copy-engine/invo-notification-executor.env

if [[ "$(id -u)" -ne 0 ]]; then
  echo "invo notification executor deployment requires root" >&2
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
  echo "$INVO_ENV does not contain INVO_ACCESS_TOKEN or INVO_REFRESH_TOKEN" >&2
  exit 2
fi
if ! command -v node >/dev/null || ! command -v npm >/dev/null; then
  echo "Node.js and npm are required on the self-hosted runner" >&2
  exit 2
fi
node_major="$(node -p 'Number(process.versions.node.split(".")[0])')"
if [[ "$node_major" -lt 20 ]]; then
  echo "Node.js >=20 required; found $(node --version)" >&2
  exit 2
fi

cd "$REPO"
git fetch origin main
git checkout main
git merge --ff-only origin/main

install -d -m 0700 "$STATE" /etc/hyperliquid-copy-engine
if [[ ! -e "$EXEC_ENV" ]]; then
  install -m 0600 /dev/null "$EXEC_ENV"
fi
chmod 0600 "$EXEC_ENV"

# Update only executor configuration keys and preserve any local bridge/secrets settings.
set_env() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  grep -v -E "^${key}=" "$EXEC_ENV" > "$tmp" || true
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  cat "$tmp" > "$EXEC_ENV"
  rm -f "$tmp"
}

# Wide research mode: no live orders. Capture every followed leaderboard trader,
# accept signals up to 25s old, mirror source leverage, and include re-ups.
set_env REAL_TRADING_ENABLED NO
set_env NOTIFICATION_TRADER_LIVE false
set_env NOTIFICATION_TRADER_ALLOW ''
set_env NOTIFICATION_TRADER_COPY_ALL_FOLLOWED true
set_env NOTIFICATION_TRADER_FEED_FILTER following
set_env NOTIFICATION_TRADER_FEED_LIMIT 30
set_env NOTIFICATION_TRADER_POLL_MS 1000
set_env NOTIFICATION_TRADER_MAX_SIGNAL_AGE_MS 25000
set_env NOTIFICATION_TRADER_MARGIN_PCT 1
set_env NOTIFICATION_TRADER_DRY_EQUITY_USD 1000

# Retained for future live execution only; these do not gate wide shadow research.
set_env NOTIFICATION_TRADER_MAX_NOTIONAL_USD 500
set_env NOTIFICATION_TRADER_MAX_SLIPPAGE_PCT 0.005
set_env NOTIFICATION_TRADER_MAX_CHASE_BPS 25
set_env NOTIFICATION_TRADER_MAX_POSITIONS 5
set_env NOTIFICATION_TRADER_HOST 127.0.0.1
set_env NOTIFICATION_TRADER_PORT 8787
set_env NOTIFICATION_TRADER_STATE_PATH /var/lib/hyperliquid-copy-engine/invo-notification-executor/state.json
set_env NOTIFICATION_TRADER_AUDIT_PATH /var/lib/hyperliquid-copy-engine/invo-notification-executor/audit.jsonl

# Remove the obsolete artificial leverage cap; source leverage is used directly.
sed -i '/^NOTIFICATION_TRADER_MAX_LEVERAGE=/d' "$EXEC_ENV"

cd "$SERVICE_DIR"
npm install --ignore-scripts --no-audit --no-fund
npm run check

install -m 0644 "$REPO/deploy/systemd/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemd-analyze verify "/etc/systemd/system/$UNIT"
systemctl enable "$UNIT"
systemctl restart "$UNIT"
sleep 2

if [[ "$(systemctl is-active "$UNIT")" != "active" ]]; then
  systemctl --no-pager --full status "$UNIT" || true
  journalctl -u "$UNIT" -n 100 --no-pager || true
  exit 1
fi

health="$(curl -fsS --max-time 3 http://127.0.0.1:8787/health)"
printf 'INVO_NOTIFICATION_EXECUTOR_HEALTH=%s\n' "$health"
systemctl --no-pager --full status "$UNIT" | head -30 || true
