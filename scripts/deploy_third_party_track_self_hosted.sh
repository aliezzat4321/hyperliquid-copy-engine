#!/usr/bin/env bash
set -euo pipefail

REPO="/root/hyperliquid-copy-engine"
PY="$REPO/.venv/bin/python"
STATE="/var/lib/hyperliquid-copy-engine/third-party"
AUDIT="/root/hyperliquid-audit/third-party"
VOLUME="/mnt/HC_Volume_106576526"
COINS="$VOLUME/hyperliquid/shadow/active_perp_markets.txt"
MARKET="$VOLUME/hyperliquid/market-shadow"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "third-party track deployment requires root" >&2
  exit 2
fi
if [[ ! -d "$REPO/.git" || ! -x "$PY" ]]; then
  echo "missing canonical repository or virtualenv under $REPO" >&2
  exit 2
fi
if ! mountpoint -q "$VOLUME"; then
  echo "Hyperliquid research volume is not mounted: $VOLUME" >&2
  exit 2
fi
for required in "$COINS" "$MARKET"; do
  if [[ ! -e "$required" ]]; then
    echo "missing required read-only market input: $required" >&2
    exit 2
  fi
done

available_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
if [[ "$available_kb" -lt 2097152 ]]; then
  echo "root filesystem has less than 2 GiB free; refusing new third-party capture" >&2
  exit 2
fi

cd "$REPO"
git fetch origin main
git checkout main
git merge --ff-only origin/main

install -d -m 0700 "$STATE" "$STATE/wide-trades" "$STATE/wide-enriched"
install -d -m 0700 "$AUDIT"

if [[ ! -s "$STATE/cutoff_ns.txt" ]]; then
  "$PY" - <<'PY' > "$STATE/cutoff_ns.txt.tmp"
import time
print(time.time_ns())
PY
  chmod 0600 "$STATE/cutoff_ns.txt.tmp"
  mv "$STATE/cutoff_ns.txt.tmp" "$STATE/cutoff_ns.txt"
fi

units=(
  hyperliquid-third-party-registry-sync.service
  hyperliquid-third-party-wide-watch.service
  hyperliquid-third-party-wide-enrichment.service
  hyperliquid-third-party-profitability.service
  hyperliquid-third-party-profitability.timer
  hyperliquid-invo-wallet-identifier.service
)
for unit in "${units[@]}"; do
  install -m 0644 "$REPO/deploy/systemd/$unit" "/etc/systemd/system/$unit"
done

systemctl daemon-reload
systemd-analyze verify \
  /etc/systemd/system/hyperliquid-third-party-registry-sync.service \
  /etc/systemd/system/hyperliquid-third-party-wide-watch.service \
  /etc/systemd/system/hyperliquid-third-party-wide-enrichment.service \
  /etc/systemd/system/hyperliquid-third-party-profitability.service \
  /etc/systemd/system/hyperliquid-third-party-profitability.timer \
  /etc/systemd/system/hyperliquid-invo-wallet-identifier.service

systemctl start hyperliquid-third-party-registry-sync.service
systemctl enable hyperliquid-third-party-wide-watch.service
systemctl enable hyperliquid-third-party-wide-enrichment.service
systemctl restart hyperliquid-third-party-wide-watch.service
systemctl restart hyperliquid-third-party-wide-enrichment.service
systemctl enable --now hyperliquid-third-party-profitability.timer

systemctl reset-failed hyperliquid-third-party-profitability.service 2>/dev/null || true
systemctl start hyperliquid-third-party-profitability.service

for service in \
  hyperliquid-third-party-wide-watch.service \
  hyperliquid-third-party-wide-enrichment.service; do
  if [[ "$(systemctl is-active "$service")" != "active" ]]; then
    systemctl --no-pager --full status "$service" || true
    journalctl -u "$service" -n 50 --no-pager || true
    exit 1
  fi
done
if [[ "$(systemctl show -p Result --value hyperliquid-third-party-profitability.service)" != "success" ]]; then
  systemctl --no-pager --full status hyperliquid-third-party-profitability.service || true
  journalctl -u hyperliquid-third-party-profitability.service -n 80 --no-pager || true
  exit 1
fi

"$PY" - "$STATE/wallets.json" "$AUDIT/third_party_scorecard.json" <<'PY'
import json
import sys
from pathlib import Path

registry_path = Path(sys.argv[1])
scorecard_path = Path(sys.argv[2])
registry = json.loads(registry_path.read_text(encoding="utf-8"))
scorecard = json.loads(scorecard_path.read_text(encoding="utf-8"))

wallets = []
for row in registry.get("wallets", []):
    if not isinstance(row, dict) or not row.get("enabled"):
        continue
    wallets.append(
        {
            "label": row.get("label"),
            "wallet": row.get("source_ref"),
            "stage": row.get("stage"),
            "enabled": row.get("enabled"),
        }
    )

scores = []
for row in scorecard.get("scorecards", []):
    if not isinstance(row, dict):
        continue
    history = row.get("historical_pre_screen")
    historical_rows = history.get("matrix", []) if isinstance(history, dict) else []
    scores.append(
        {
            "source": row.get("source"),
            "identity": row.get("identity"),
            "label": row.get("label"),
            "wallet": row.get("wallet"),
            "prospective_events": row.get("prospective_events"),
            "status": row.get("status"),
            "copyability_score": row.get("copyability_score"),
            "validation_candidate": row.get("validation_candidate"),
            "historical_pre_screen_available": bool(
                isinstance(history, dict) and history.get("available")
            ),
            "historical_matrix_rows": len(historical_rows),
        }
    )

print(
    "THIRD_PARTY_TRACK_STATUS="
    + json.dumps(
        {
            "wallets": wallets,
            "scores": scores,
            "watch_service": "active",
            "enrichment_service": "active",
            "real_trading": False,
        },
        sort_keys=True,
    )
)
PY

systemctl list-timers hyperliquid-third-party-profitability.timer --no-pager || true
df -h /
