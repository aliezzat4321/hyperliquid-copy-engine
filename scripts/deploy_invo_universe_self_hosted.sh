#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/root/hyperliquid-copy-engine}"
STATE_DIR="/var/lib/hyperliquid-copy-engine/invo"
INVO_ENV="/etc/hyperliquid-copy-engine/invo.env"
SERVICE="hyperliquid-invo-universe-miner.service"
DIRECT_SERVICE="hyperliquid-invo-direct-history.service"
TIMER="hyperliquid-invo-universe-miner.timer"

if [[ "${EUID}" -ne 0 ]]; then
  echo "deployment must run as root" >&2
  exit 1
fi
for required in "${REPO_DIR}/.git" "${REPO_DIR}/.venv/bin/python" "${INVO_ENV}"; do
  [[ -e "${required}" ]] || { echo "missing ${required}" >&2; exit 1; }
done

install -d -m 0700 -o root -g root "${STATE_DIR}"
cd "${REPO_DIR}"
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "refusing deployment with tracked local changes" >&2
  exit 1
fi

git fetch origin main
git checkout main
git merge --ff-only origin/main

for unit in "${SERVICE}" "${DIRECT_SERVICE}"; do
  sed "s|/root/hyperliquid-copy-engine|${REPO_DIR}|g" \
    "deploy/systemd/${unit}" >"/etc/systemd/system/${unit}"
  chmod 0644 "/etc/systemd/system/${unit}"
done
install -m 0644 "deploy/systemd/${TIMER}" "/etc/systemd/system/${TIMER}"
systemctl daemon-reload
systemd-analyze verify \
  "/etc/systemd/system/${SERVICE}" \
  "/etc/systemd/system/${DIRECT_SERVICE}"
systemctl enable --now "${TIMER}"

systemctl start "${SERVICE}"
result="$(systemctl show --property=Result --value "${SERVICE}")"
if [[ "${result}" != "success" ]]; then
  systemctl status "${SERVICE}" --no-pager -l >&2 || true
  journalctl -u "${SERVICE}" -n 160 --no-pager >&2 || true
  exit 1
fi

# OnSuccess from the universe miner starts this automatically. Starting it again is
# idempotent and makes deployment wait until the direct-history queue is materialized.
systemctl start "${DIRECT_SERVICE}"
direct_result="$(systemctl show --property=Result --value "${DIRECT_SERVICE}")"
if [[ "${direct_result}" != "success" ]]; then
  systemctl status "${DIRECT_SERVICE}" --no-pager -l >&2 || true
  journalctl -u "${DIRECT_SERVICE}" -n 160 --no-pager >&2 || true
  exit 1
fi

"${REPO_DIR}/.venv/bin/python" - \
  "${STATE_DIR}/universe_candidates.json" \
  "${STATE_DIR}/resolution_queue/resolution_queue.json" <<'PY'
import json
import sys
from pathlib import Path

universe_path = Path(sys.argv[1])
queue_path = Path(sys.argv[2])
payload = json.loads(universe_path.read_text(encoding="utf-8"))
queue = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else {}
rows = []
for row in payload.get("candidates", [])[:25]:
    if not isinstance(row, dict):
        continue
    rows.append({
        "username": row.get("username"),
        "portfolio_name": row.get("name"),
        "portfolio_id": row.get("portfolio_id"),
        "leaderboard_timeframes": row.get("leaderboard_timeframes"),
        "closed_positions": row.get("closed_positions"),
        "win_rate": row.get("win_rate"),
        "percent_change": row.get("percent_change"),
        "verified_trade_posts": row.get("verified_trade_posts"),
        "evidence_count": row.get("evidence_count"),
        "tracking_stage": row.get("tracking_stage"),
        "screen_score": row.get("screen_score"),
    })
print("INVO_UNIVERSE_STATUS=" + json.dumps({
    "candidate_portfolio_count": payload.get("candidate_portfolio_count"),
    "candidate_owner_count": payload.get("candidate_owner_count"),
    "discovered_owner_count": payload.get("discovered_owner_count"),
    "ready_for_wallet_resolution": payload.get("ready_for_wallet_resolution"),
    "resolution_queue_count": queue.get("ready_count", payload.get("resolution_queue_count")),
    "direct_history": payload.get("direct_history"),
    "surface_errors": payload.get("surface_errors"),
    "top_25": rows,
}, sort_keys=True))
PY

systemctl list-timers --all --no-pager "${TIMER}"
