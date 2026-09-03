#!/usr/bin/env bash
set -euo pipefail

DECISION="${HLCOPY_STORAGE_DECISION:-/run/hlcopy-storage-controller/decision.json}"
MAX_AGE_SECONDS="${HLCOPY_STORAGE_DECISION_MAX_AGE_SECONDS:-7200}"

# Compatibility shim only: it stops on pressure or invalid state and never resumes.
action="$(python3 - "$DECISION" "$MAX_AGE_SECONDS" <<'PY'
import json, os, sys, time
try:
    path, max_age = sys.argv[1], int(sys.argv[2])
    if time.time() - os.stat(path).st_mtime > max_age:
        raise ValueError("stale")
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    action = value.get("action")
    if action not in {"ALLOW", "WARN", "STOP_ALL_MATERIAL_WRITERS"}:
        raise ValueError("invalid")
    print(action)
except Exception:
    print("STOP_ALL_MATERIAL_WRITERS")
PY
)"
echo "STORAGE_GUARD controller_action=$action decision=$DECISION"
if [[ "$action" == "STOP_ALL_MATERIAL_WRITERS" ]]; then
  echo "STORAGE_GUARD=STOP_WRITER reason=controller_decision"
  systemctl stop hyperliquid-market-capture.timer 2>/dev/null || true
  systemctl stop hyperliquid-market-capture.service 2>/dev/null || true
  exit 0
fi
echo "STORAGE_GUARD=$action resume_authority=manager"
