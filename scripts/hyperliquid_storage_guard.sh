#!/usr/bin/env bash
set -euo pipefail

MOUNT="${HLCOPY_STORAGE_MOUNT:-/mnt/HC_Volume_106576526}"
WARN_PCT="${HLCOPY_STORAGE_WARN_PCT:-75}"
STOP_PCT="${HLCOPY_STORAGE_STOP_PCT:-85}"
RESUME_PCT="${HLCOPY_STORAGE_RESUME_PCT:-78}"
STATE_DIR="${HLCOPY_STORAGE_GUARD_STATE_DIR:-/run/hlcopy-storage-guard}"
SENTINEL="$STATE_DIR/market_capture_paused"
mkdir -p "$STATE_DIR"

usage_pct="$(df -P "$MOUNT" | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
if [[ ! "$usage_pct" =~ ^[0-9]+$ ]]; then
  echo "STORAGE_GUARD=ERROR unable_to_parse_usage mount=$MOUNT" >&2
  exit 2
fi

echo "STORAGE_GUARD usage_pct=$usage_pct warn_pct=$WARN_PCT stop_pct=$STOP_PCT resume_pct=$RESUME_PCT mount=$MOUNT"

if (( usage_pct >= STOP_PCT )); then
  echo "STORAGE_GUARD=STOP_WRITER reason=disk_pressure"
  systemctl stop hyperliquid-market-capture.timer 2>/dev/null || true
  systemctl stop hyperliquid-market-capture.service 2>/dev/null || true
  date -u +%FT%TZ > "$SENTINEL"
  exit 0
fi

if (( usage_pct >= WARN_PCT )); then
  echo "STORAGE_GUARD=WARN disk_usage=${usage_pct}%"
else
  echo "STORAGE_GUARD=OK disk_usage=${usage_pct}%"
fi

# Hysteresis: only re-enable the timer if this guard was the component that
# previously paused it, and disk usage has fallen safely below the resume mark.
if [[ -f "$SENTINEL" ]] && (( usage_pct <= RESUME_PCT )); then
  echo "STORAGE_GUARD=RESUME_MARKET_CAPTURE"
  systemctl start hyperliquid-market-capture.timer 2>/dev/null || true
  systemctl start hyperliquid-market-capture.service 2>/dev/null || true
  rm -f "$SENTINEL"
fi
