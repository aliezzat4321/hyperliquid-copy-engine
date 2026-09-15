#!/usr/bin/env bash
set -euo pipefail

REVIEW_SHA="${1:-${REVIEW_SHA:-}}"
SOURCE_REPO="${GITHUB_WORKSPACE:-$(pwd)}"
WIDE_DIR="/mnt/HC_Volume_106576526/hyperliquid/shadow/wide-enriched-live"
CUTOFF_FILE="/mnt/HC_Volume_106576526/hyperliquid/shadow/wide_clean_cutoff_ns.txt"
MARKET_DIR="/mnt/HC_Volume_106576526/hyperliquid/market-shadow"
UNIVERSE_STATE="/mnt/HC_Volume_106576526/hyperliquid/discovery/universe_state.json"
CANONICAL_EVIDENCE="/root/hyperliquid-audit/evidence"
BOOTSTRAP_PY="${HLCOPY_REVIEW_PYTHON:-/root/hyperliquid-copy-engine/.venv/bin/python}"

if [[ "${REAL_TRADING_ENABLED:-NO}" == "YES" ]]; then
  echo "REAL_TRADING_ENABLED must remain NO" >&2
  exit 1
fi
if [[ ! "$REVIEW_SHA" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "review SHA must be a full 40-character commit SHA" >&2
  exit 2
fi
if [[ ! -f "$SOURCE_REPO/pyproject.toml" ]]; then
  echo "review source repo missing pyproject.toml: $SOURCE_REPO" >&2
  exit 3
fi
for required in "$WIDE_DIR" "$CUTOFF_FILE" "$MARKET_DIR" "$UNIVERSE_STATE"; do
  if [[ ! -e "$required" ]]; then
    echo "required shadow evidence input missing: $required" >&2
    exit 4
  fi
done
if [[ ! -x "$BOOTSTRAP_PY" ]]; then
  echo "Python >=3.12 bootstrap interpreter missing: $BOOTSTRAP_PY" >&2
  exit 5
fi
if ! "$BOOTSTRAP_PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "review bootstrap Python must be >=3.12: $BOOTSTRAP_PY" >&2
  "$BOOTSTRAP_PY" --version >&2 || true
  exit 5
fi

SHORT_SHA="${REVIEW_SHA:0:12}"
RUNTIME_ROOT="/root/hyperliquid-review-runtime/$REVIEW_SHA"
RUNTIME_REPO="$RUNTIME_ROOT/repo"
VENV="$RUNTIME_ROOT/.venv"
AUDIT_ROOT="/root/hyperliquid-audit/reviews/$REVIEW_SHA"
FUNNEL_OUT="$AUDIT_ROOT/funnel"
PROSPECTIVE_OUT="$AUDIT_ROOT/prospective"
IMMUTABLE_EVIDENCE="$AUDIT_ROOT/evidence"

mkdir -p "$RUNTIME_ROOT" "$AUDIT_ROOT"
rsync -a --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  "$SOURCE_REPO/" "$RUNTIME_REPO/"

# A prior failed review may have left a Python 3.10 venv behind. Never reuse an
# incompatible environment: destroy it and recreate from the managed >=3.12 runtime.
if [[ -x "$VENV/bin/python" ]] && \
   ! "$VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  rm -rf "$VENV"
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  "$BOOTSTRAP_PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install -q -U pip
"$VENV/bin/python" -m pip install -q -e "$RUNTIME_REPO"
PY="$VENV/bin/python"

echo "lane1_review_python=$($PY --version 2>&1) bootstrap=$BOOTSTRAP_PY"

export REAL_TRADING_ENABLED=NO
export HLCOPY_DEPLOYED_GIT_SHA="$REVIEW_SHA"

rm -rf "$FUNNEL_OUT" "$PROSPECTIVE_OUT" "$IMMUTABLE_EVIDENCE"
mkdir -p "$FUNNEL_OUT" "$PROSPECTIVE_OUT"

run_stage() {
  local stage="$1"
  shift
  local unit="hlcopy-lane1-review-${stage}-${SHORT_SHA}"
  systemctl stop "$unit.service" >/dev/null 2>&1 || true
  systemctl reset-failed "$unit.service" >/dev/null 2>&1 || true
  systemd-run \
    --quiet \
    --wait \
    --pipe \
    --collect \
    --unit="$unit" \
    --working-directory="$RUNTIME_REPO" \
    --property=MemoryMax=1400M \
    --property=Environment=REAL_TRADING_ENABLED=NO \
    --property=Environment=HLCOPY_DEPLOYED_GIT_SHA="$REVIEW_SHA" \
    "$@"
}

echo "lane1_review_start sha=$REVIEW_SHA real_trading=NO"

run_stage funnel \
  "$PY" -m hlcopy.profitability.incremental_funnel_cli \
  --wide-enriched-dir "$WIDE_DIR" \
  --wide-cutoff-ns-file "$CUTOFF_FILE" \
  --market-dir "$MARKET_DIR" \
  --output-dir "$FUNNEL_OUT" \
  --universe-state "$UNIVERSE_STATE" \
  --prospective-report "$PROSPECTIVE_OUT/report.json"

run_stage prospective \
  "$PY" "$RUNTIME_REPO/scripts/run_prospective_champion_review.py" \
  --challenger-queue "$FUNNEL_OUT/challenger_queue.json" \
  --output-dir "$PROSPECTIVE_OUT" \
  --wide-enriched-dir "$WIDE_DIR" \
  --market-dir "$MARKET_DIR"

run_stage evidence \
  "$PY" -m hlcopy.profitability.lane1_audit_bundle \
  --challenger-queue "$FUNNEL_OUT/challenger_queue.json" \
  --funnel-report "$FUNNEL_OUT/funnel_report.json" \
  --prospective-report "$PROSPECTIVE_OUT/report.json" \
  --wide-enriched-dir "$WIDE_DIR" \
  --wide-cutoff-ns-file "$CUTOFF_FILE" \
  --market-dir "$MARKET_DIR" \
  --output-dir "$CANONICAL_EVIDENCE" \
  --git-sha "$REVIEW_SHA"

# Preserve the complete replay evidence under the exact reviewed SHA before any
# later run can replace the canonical evidence path.
cp -a "$CANONICAL_EVIDENCE" "$IMMUTABLE_EVIDENCE"
cp "$IMMUTABLE_EVIDENCE/manifest.json" "$AUDIT_ROOT/evidence-manifest.json"

"$PY" - "$FUNNEL_OUT/funnel_report.json" "$PROSPECTIVE_OUT/report.json" "$IMMUTABLE_EVIDENCE/manifest.json" <<'PY'
import json
import sys
from pathlib import Path

funnel = json.loads(Path(sys.argv[1]).read_text())
prospective = json.loads(Path(sys.argv[2]).read_text())
manifest = json.loads(Path(sys.argv[3]).read_text())

assert funnel.get("real_trading") is False
assert prospective.get("real_trading") is False
assert manifest.get("real_trading") is False
assert manifest.get("git_sha")
assert manifest.get("target_count", 0) > 0

print(
    "lane1_review_complete",
    f"return_basis={funnel.get('return_basis')}",
    f"robust={funnel.get('robust_candidate_count')}",
    f"challengers={funnel.get('boundary_counts', {}).get('challenger')}",
    f"prospective_evaluated={prospective.get('evaluated_count')}",
    f"prospective_approved={prospective.get('approved_count')}",
    f"evidence_targets={manifest.get('target_count')}",
    "real_trading=NO",
)
PY
