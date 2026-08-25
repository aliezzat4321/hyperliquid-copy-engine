#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/root/hyperliquid-copy-engine}"
STATE_DIR="/var/lib/hyperliquid-copy-engine/invo"
INVO_ENV="/etc/hyperliquid-copy-engine/invo.env"
SERVICE="hyperliquid-invo-universe-miner.service"
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

sed "s|/root/hyperliquid-copy-engine|${REPO_DIR}|g" \
  "deploy/systemd/${SERVICE}" >"/etc/systemd/system/${SERVICE}"
install -m 0644 "deploy/systemd/${TIMER}" "/etc/systemd/system/${TIMER}"
systemctl daemon-reload
systemd-analyze verify "/etc/systemd/system/${SERVICE}"
systemctl enable --now "${TIMER}"

systemctl start "${SERVICE}"
result="$(systemctl show --property=Result --value "${SERVICE}")"
if [[ "${result}" != "success" ]]; then
  systemctl status "${SERVICE}" --no-pager -l >&2 || true
  journalctl -u "${SERVICE}" -n 160 --no-pager >&2 || true
  exit 1
fi

"${REPO_DIR}/.venv/bin/python" - "${STATE_DIR}/universe_candidates.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
rows = []
for row in payload.get("candidates", [])[:25]:
    if not isinstance(row, dict):
        continue
    rows.append({
        "username": row.get("username"),
        "portfolio_id": row.get("portfolio_id"),
        "closed_positions": row.get("closed_positions"),
        "win_rate": row.get("win_rate"),
        "percent_change": row.get("percent_change"),
        "verified_trade_posts": row.get("verified_trade_posts"),
        "evidence_count": row.get("evidence_count"),
        "tracking_stage": row.get("tracking_stage"),
        "screen_score": row.get("screen_score"),
    })
print("INVO_UNIVERSE_STATUS=" + json.dumps({
    "candidate_count": payload.get("candidate_count"),
    "ready_for_wallet_resolution": payload.get("ready_for_wallet_resolution"),
    "resolution_queue_count": payload.get("resolution_queue_count"),
    "surface_errors": payload.get("surface_errors"),
    "top_25": rows,
}, sort_keys=True))
PY

systemctl list-timers --all --no-pager "${TIMER}"
