#!/usr/bin/env bash
set -euo pipefail

REPO=/root/hyperliquid-copy-engine
SERVICE_REL=services/invo-notification-executor
SERVICE_DIR="$REPO/$SERVICE_REL"
UNIT=hyperliquid-invo-notification-executor.service
RESEARCH_SERVICE=hyperliquid-invo-portfolio-research.service
RESEARCH_TIMER=hyperliquid-invo-portfolio-research.timer
STATE=/var/lib/hyperliquid-copy-engine/invo-notification-executor
INVO_ENV=/etc/hyperliquid-copy-engine/invo.env
EXEC_ENV=/etc/hyperliquid-copy-engine/invo-notification-executor.env
RESET_SCRIPT="$REPO/scripts/reset_lane3_shadow_epoch.py"
# Start the repaired selector/execution model from one clean prospective epoch.
# Subsequent deploys inside this same epoch must preserve the observation window.
EVIDENCE_EPOCH=lane3-hybrid-v3-clean-20260916
EVIDENCE_MARKER="$STATE/evidence-epoch"
EXPECTED_SELECTOR=invo-portfolio-hybrid-v3-20260916

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
set_env NOTIFICATION_TRADER_FUNDING_BOUNDARY_PATH /var/lib/hyperliquid-copy-engine/invo-notification-executor/funding-boundaries
set_env NOTIFICATION_TRADER_TRACKER_PATH /var/lib/hyperliquid-copy-engine/invo-notification-executor/trader-population.json
set_env NOTIFICATION_TRADER_CANDIDATE_STATE_PATH /var/lib/hyperliquid-copy-engine/invo-notification-executor/portfolio-candidates.json
set_env NOTIFICATION_TRADER_DIRECT_WATCH_STATE_PATH /var/lib/hyperliquid-copy-engine/invo-notification-executor/elite-direct-watch.json
set_env NOTIFICATION_TRADER_DIRECT_WATCH_ADMISSION_INDEX_PATH /var/lib/hyperliquid-copy-engine/invo-notification-executor/elite-direct-watch-admissions.json
set_env INVO_HTTP_REQUEST_TIMEOUT_MS 2000
set_env DIRECT_WATCH_FIXED_OVERHEAD_MS 2000
set_env DIRECT_WATCH_CONCURRENCY 16
set_env DIRECT_WATCH_MAX_REQUESTS_PER_SECOND 12
set_env DIRECT_WATCH_REQUEST_BURST 32
set_env DIRECT_WATCH_FIXED_RESERVE_REQUESTS_PER_SECOND 4
set_env NOTIFICATION_TRADER_DIRECT_WATCH_SCAN_MS 3000
set_env NOTIFICATION_TRADER_DIRECT_WATCH_MAX_HYDRATES_PER_SCAN 24
set_env NOTIFICATION_TRADER_DIRECT_WATCH_OPEN_MAX_PAGES 3
set_env NOTIFICATION_TRADER_DIRECT_WATCH_FALLBACK_POLL_MS 18000
set_env NOTIFICATION_TRADER_DIRECT_WATCH_CLOSED_POLL_MS 60000
set_env NOTIFICATION_TRADER_DIRECT_WATCH_MAX_CLOSED_HYDRATES_PER_SCAN 24
set_env NOTIFICATION_TRADER_DIRECT_WATCH_CLOSED_MAX_PAGES 2
set_env MAX_DIRECT_WATCH_RESIDENT_TARGETS 48
set_env DIRECT_WATCH_NEGATIVE_MIN_OBSERVATIONS 2
set_env DIRECT_WATCH_NEGATIVE_GRACE_MS 600000
# The clean-epoch seed runs the portfolio CLI directly rather than through its
# systemd unit, so persist the CLI's own path variables in the sourced env file.
set_env INVO_PORTFOLIO_CANDIDATE_STATE_PATH /var/lib/hyperliquid-copy-engine/invo-notification-executor/portfolio-candidates.json
set_env INVO_PORTFOLIO_CANDIDATE_SNAPSHOTS_PATH /var/lib/hyperliquid-copy-engine/invo-notification-executor/portfolio-candidate-snapshots.jsonl
set_env INVO_PORTFOLIO_LEADERBOARD_STATE_PATH /var/lib/hyperliquid-copy-engine/invo-notification-executor/invo-leaderboards.json
set_env INVO_PORTFOLIO_LEADERBOARD_SNAPSHOTS_PATH /var/lib/hyperliquid-copy-engine/invo-notification-executor/invo-leaderboard-snapshots.jsonl
# Exact current web-app enum values, live-probed read-only on /v1_0/posts/get_feed.
# Each surface receives its own durable prospective baseline before OPEN/ADD processing.
set_env NOTIFICATION_TRADER_DISCOVERY_SURFACES following,trending,fire_moves,most_recent

# Remove the obsolete artificial leverage cap; source leverage is used directly.
sed -i '/^NOTIFICATION_TRADER_MAX_LEVERAGE=/d' "$EXEC_ENV"

cd "$SERVICE_DIR"
npm install --ignore-scripts --no-audit --no-fund
npm run check

# Reset only when crossing into the repaired v3 measurement epoch. Never reset on
# ordinary redeploys. The reset preserves source discovery plus seen/feed cursors,
# clears simulated managed exposure, and deletes superseded derived economics.
reset_evidence=0
reset_deferred=0
current_epoch=""
if [[ -f "$EVIDENCE_MARKER" ]]; then
  current_epoch="$(cat "$EVIDENCE_MARKER")"
fi
selector_ready=0
if grep -Fq "$EXPECTED_SELECTOR" "$SERVICE_DIR/src/portfolio-candidates.ts"; then
  selector_ready=1
fi

# A rollback after the clean epoch has started must never silently resume an old
# selector against v3 evidence.
if [[ "$current_epoch" == "$EVIDENCE_EPOCH" && "$selector_ready" -ne 1 ]]; then
  echo "refusing Lane 3 selector rollback after clean v3 epoch started" >&2
  exit 3
fi

if [[ "$current_epoch" != "$EVIDENCE_EPOCH" ]]; then
  if [[ "$selector_ready" -ne 1 ]]; then
    # It is safe to merge/deploy this guard before PR #373: keep the current
    # epoch untouched until the repaired selector arrives on canonical main.
    reset_deferred=1
    echo "INVO_NOTIFICATION_EXECUTOR_EVIDENCE_RESET_DEFERRED=selector_v3_not_deployed"
  else
    if [[ ! -f "$RESET_SCRIPT" ]]; then
      echo "missing Lane 3 reset helper: $RESET_SCRIPT" >&2
      exit 2
    fi

    # Quiesce only Lane 3 writers while the epoch boundary is cut. Do not touch
    # market-data, trading, other lanes, credentials, or host services.
    systemctl stop "$RESEARCH_TIMER" 2>/dev/null || true
    systemctl stop "$RESEARCH_SERVICE" 2>/dev/null || true
    systemctl stop "$UNIT" 2>/dev/null || true

    python3 "$RESET_SCRIPT" \
      --state-root "$STATE" \
      --env-file "$EXEC_ENV" \
      --epoch "$EVIDENCE_EPOCH"
    reset_evidence=1

    # Seed selector-v3 eligibility before the executor can accept a NEW/ADD event.
    # Match the research systemd unit's runtime environment so the direct CLI writes
    # the production candidate/leaderboard paths rather than repository-local defaults.
    # Both files are root-owned deployment inputs; a malformed file fails closed under
    # `set -e` before the executor is restarted or the clean epoch is marked complete.
    set -a
    # shellcheck disable=SC1090
    source "$INVO_ENV"
    # shellcheck disable=SC1090
    source "$EXEC_ENV"
    set +a

    # This creates fresh candidate state/snapshots while retaining raw leaderboard history.
    node dist/src/portfolio-candidate-cli.js
    node - "$STATE/portfolio-candidates.json" "$EXPECTED_SELECTOR" <<'NODE'
const fs = require('fs');
const [candidatePath, expectedSelector] = process.argv.slice(2);
const candidate = JSON.parse(fs.readFileSync(candidatePath, 'utf8'));
if (candidate.selectorVersion !== expectedSelector) {
  throw new Error(
    `clean epoch seeded unexpected selector ${candidate.selectorVersion}; expected ${expectedSelector}`,
  );
}
NODE
  fi
fi

install -m 0644 "$REPO/deploy/systemd/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemd-analyze verify "/etc/systemd/system/$UNIT"
systemctl enable "$UNIT"
systemctl restart "$UNIT"

# Startup performs token/feed hydration before binding HTTP. Use the lightweight /traders
# endpoint for readiness; /health performs portfolio-wide MTM/funding I/O and is intentionally
# checked separately with a much larger timeout. This separation keeps deployment verification
# strict without confusing slow economic diagnostics with service unavailability.
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

# If this deploy cut the clean epoch, resume installed portfolio research only
# after the repaired executor is healthy. A separate deployment owns installation.
if [[ "$reset_evidence" -eq 1 ]]; then
  if systemctl cat "$RESEARCH_SERVICE" >/dev/null 2>&1; then
    systemctl start "$RESEARCH_SERVICE"
  fi
  if systemctl cat "$RESEARCH_TIMER" >/dev/null 2>&1; then
    systemctl restart "$RESEARCH_TIMER"
  fi
fi

# Mark the evidence epoch only after a real v3 reset plus successful service
# health. A pre-v3 deployment deliberately leaves the previous marker untouched.
if [[ "$reset_deferred" -eq 0 ]]; then
  printf '%s\n' "$EVIDENCE_EPOCH" > "$EVIDENCE_MARKER"
fi
printf 'INVO_NOTIFICATION_EXECUTOR_EVIDENCE_RESET=%s\n' "$reset_evidence"
printf 'INVO_NOTIFICATION_EXECUTOR_EVIDENCE_RESET_DEFERRED=%s\n' "$reset_deferred"
printf 'INVO_NOTIFICATION_EXECUTOR_EVIDENCE_EPOCH=%s\n' "$EVIDENCE_EPOCH"
printf 'INVO_NOTIFICATION_EXECUTOR_READINESS=%s\n' "$readiness"
printf 'INVO_NOTIFICATION_EXECUTOR_HEALTH=%s\n' "$health"
systemctl --no-pager --full status "$UNIT" | head -30 || true
