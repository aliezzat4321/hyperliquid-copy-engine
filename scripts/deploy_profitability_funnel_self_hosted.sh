#!/usr/bin/env bash
set -euo pipefail

REPO="/root/hyperliquid-copy-engine"
PY="$REPO/.venv/bin/python"
OUT="/root/hyperliquid-audit/funnel"
UNIT="hlcopy-profitability-funnel.service"
WIDE="/mnt/HC_Volume_106576526/hyperliquid/shadow/wide-enriched-live"
CUTOFF="/mnt/HC_Volume_106576526/hyperliquid/shadow/wide_clean_cutoff_ns.txt"
MARKET="/mnt/HC_Volume_106576526/hyperliquid/market-shadow"

cd "$REPO"

if [[ ! -x "$PY" ]]; then
  echo "missing python: $PY" >&2
  exit 2
fi
for required in "$WIDE" "$CUTOFF" "$MARKET"; do
  if [[ ! -e "$required" ]]; then
    echo "missing required input: $required" >&2
    exit 2
  fi
done

mkdir -p "$OUT"

systemctl stop "$UNIT" 2>/dev/null || true
systemctl reset-failed "$UNIT" 2>/dev/null || true

systemd-run \
  --unit=hlcopy-profitability-funnel \
  --description="Hyperliquid incremental profitability funnel" \
  --property=RuntimeMaxSec=21600 \
  --property=MemoryMax=1200M \
  --property=Environment=REAL_TRADING_ENABLED=NO \
  --working-directory="$REPO" \
  "$PY" \
  -m hlcopy.profitability.incremental_funnel_cli \
  --wide-enriched-dir "$WIDE" \
  --wide-cutoff-ns-file "$CUTOFF" \
  --market-dir "$MARKET" \
  --output-dir "$OUT"

sleep 3
state="$(systemctl is-active "$UNIT" || true)"
echo "funnel_service_state=$state"
systemctl --no-pager --full status "$UNIT" | head -25 || true
journalctl -u "$UNIT" -n 20 --no-pager || true

if [[ "$state" != "active" ]]; then
  echo "profitability funnel failed to remain active" >&2
  exit 1
fi

echo "PROFITABILITY_FUNNEL_STARTED=YES"
