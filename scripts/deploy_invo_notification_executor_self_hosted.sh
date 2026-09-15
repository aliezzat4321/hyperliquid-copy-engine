#!/usr/bin/env bash
set -euo pipefail

REPO=/root/hyperliquid-copy-engine
SERVICE_REL=services/invo-notification-executor
SERVICE_DIR="$REPO/$SERVICE_REL"
UNIT=hyperliquid-invo-notification-executor.service
STATE=/var/lib/hyperliquid-copy-engine/invo-notification-executor
INVO_ENV=/etc/hyperliquid-copy-engine/invo.env
EXEC_ENV=/etc/hyperliquid-copy-engine/invo-notification-executor.env
# Reset prospective Lane-3 evidence exactly once for the repaired causal-L2/funding-completeness model.
# Subsequent code/config deploys must preserve the accumulating observation window.
EVIDENCE_EPOCH=lane3-causal-l2-v2-funding-complete-20260915
EVIDENCE_MARKER="$STATE/evidence-epoch"

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
# npm/build output is disposable. Clean only known generated paths before fast-forwarding so
# locally generated files (including ignored node_modules/dist) cannot block tracked files
# arriving from main. Tracked files are never removed by git clean, even with -x.
git clean -fdx -- \
  "$SERVICE_REL/package-lock.json" \
  "$SERVICE_REL/node_modules" \
  "$SERVICE_REL/dist"
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
set_env NOTIFICATION_TRADER_TRACKER_PATH /var/lib/hyperliquid-copy-engine/invo-notification-executor/trader-population.json
# Current Invo feed API accepts following/trending; `all` returns HTTP 500 Invalid feed type.
# Keep discovery broad across valid surfaces without flooding the executor with known-bad requests.
set_env NOTIFICATION_TRADER_DISCOVERY_SURFACES following,trending

# Remove the obsolete artificial leverage cap; source leverage is used directly.
sed -i '/^NOTIFICATION_TRADER_MAX_LEVERAGE=/d' "$EXEC_ENV"

cd "$SERVICE_DIR"
npm install --ignore-scripts --no-audit --no-fund
npm run check

# Start a clean prospective evidence epoch only when the evidence model changes. Preserve
# future deploys inside the same epoch so the >=7-day / >=20-event observation window accrues.
reset_evidence=0
current_epoch=""
if [[ -f "$EVIDENCE_MARKER" ]]; then
  current_epoch="$(cat "$EVIDENCE_MARKER")"
fi
if [[ "$current_epoch" != "$EVIDENCE_EPOCH" ]]; then
  reset_evidence=1
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ -f "$STATE/state.json" ]]; then
    mv "$STATE/state.json" "$STATE/state.pre-${EVIDENCE_EPOCH}-${stamp}.json"
  fi
  if [[ -f "$STATE/audit.jsonl" ]]; then
    mv "$STATE/audit.jsonl" "$STATE/audit.pre-${EVIDENCE_EPOCH}-${stamp}.jsonl"
  fi
  if [[ -f "$STATE/trader-population.json" ]]; then
    mv "$STATE/trader-population.json" "$STATE/trader-population.pre-${EVIDENCE_EPOCH}-${stamp}.json"
  fi
fi

install -m 0644 "$REPO/deploy/systemd/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemd-analyze verify "/etc/systemd/system/$UNIT"
systemctl enable "$UNIT"
systemctl restart "$UNIT"

# Startup performs token/feed hydration before binding HTTP. Use the lightweight /traders
# endpoint for readiness; /health performs portfolio-wide MTM/funding I/O and is intentionally
# checked separately with a much larger timeout.
readiness=""
for attempt in $(seq 1 30); do
  if [[ "$(systemctl is-active "$UNIT")" != "active" ]]; then
    echo "Lane 3 service became inactive during readiness attempt ${attempt}" >&2
    systemctl --no-pager --full status "$UNIT" || true
    journalctl -u "$UNIT" -n 100 --no-pager || true
    exit 1
  fi
  if readiness="$(curl -fsS --max-time 3 http://127.0.0.1:8787/traders 2>/dev/null)"; then
    break
  fi
  sleep 1
done

if [[ -z "$readiness" ]]; then
  echo "Lane 3 service stayed active but HTTP readiness never completed within the bounded startup window" >&2
  systemctl --no-pager --full status "$UNIT" || true
  journalctl -u "$UNIT" -n 100 --no-pager || true
  exit 1
fi

health=""
if ! health="$(curl -fsS --max-time 60 http://127.0.0.1:8787/health)"; then
  echo "Lane 3 service is ready, but full portfolio health/MTM proof did not complete within 60s" >&2
  systemctl --no-pager --full status "$UNIT" || true
  journalctl -u "$UNIT" -n 100 --no-pager || true
  exit 1
fi

# Mark the evidence epoch only after the service is ready and full portfolio health succeeds,
# so a failed deployment cannot falsely claim that the observation window is healthy.
printf '%s\n' "$EVIDENCE_EPOCH" > "$EVIDENCE_MARKER"
printf 'INVO_NOTIFICATION_EXECUTOR_EVIDENCE_RESET=%s\n' "$reset_evidence"
printf 'INVO_NOTIFICATION_EXECUTOR_EVIDENCE_EPOCH=%s\n' "$EVIDENCE_EPOCH"
printf 'INVO_NOTIFICATION_EXECUTOR_READINESS=%s\n' "$readiness"
printf 'INVO_NOTIFICATION_EXECUTOR_HEALTH=%s\n' "$health"
systemctl --no-pager --full status "$UNIT" | head -30 || true
