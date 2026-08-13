#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

unit_dir="/etc/systemd/system"
units=(
  hyperliquid-shadow-validation.service
  hyperliquid-wallet-research.service
  hyperliquid-wallet-research.timer
  hyperliquid-profitability.service
  hyperliquid-profitability.timer
  hyperliquid-selective-shadow.service
  hyperliquid-selective-shadow.timer
)

for unit in "${units[@]}"; do
  src="$repo_dir/deploy/systemd/$unit"
  [[ -f "$src" ]] || { echo "missing unit: $src" >&2; exit 1; }
  grep -q 'REAL_TRADING_ENABLED=NO' "$src" || {
    if [[ "$unit" == *.service ]]; then
      echo "service lacks REAL_TRADING_ENABLED=NO: $unit" >&2
      exit 1
    fi
  }
  install -m 0644 "$src" "$unit_dir/$unit"
done

# Establish/refresh prospective margin truth first. The installer now snapshots
# default plus every discovered HIP-3 perp DEX; no later metadata is backfilled.
bash "$repo_dir/deploy/install_margin_snapshot_timer.sh"

systemctl daemon-reload

# Capture is a permanent daemon. Restart to activate l2Book + activeAssetCtx.
systemctl enable hyperliquid-shadow-validation.service
systemctl restart hyperliquid-shadow-validation.service

# Baseline research stays independent from selective shadow. Generate one fresh,
# fee-complete attribution artifact before creating the first future-only policy.
systemctl stop hyperliquid-profitability.timer || true
systemctl start hyperliquid-profitability.service

# Policy publication happens only after the baseline scorer has finished.
systemctl stop hyperliquid-wallet-research.timer || true
systemctl start hyperliquid-wallet-research.service

policy_store="/mnt/HC_Volume_106576526/hyperliquid/shadow/selective_policies.json"
[[ -s "$policy_store" ]] || {
  echo "no selective policy was published; refusing to enable selective shadow" >&2
  exit 1
}

REAL_TRADING_ENABLED=NO "$repo_dir/.venv/bin/python" - <<'PY'
from pathlib import Path
from hlcopy.shadow.selective_policy import load_policy_store

path = Path('/mnt/HC_Volume_106576526/hyperliquid/shadow/selective_policies.json')
store = load_policy_store(path)
assert store.policies, 'empty policy store'
latest = store.policies[-1]
assert latest.research_only is True
assert latest.training_end_ns < latest.effective_from_ns
assert all(rule.state == 'SHADOW_ONLY' for rule in latest.rules)
print(
    f"policy_ready id={latest.policy_id} rules={len(latest.rules)} "
    f"training_end_ns={latest.training_end_ns} effective_from_ns={latest.effective_from_ns}"
)
PY

# Enable recurring loops only after bootstrap artifacts have passed validation.
systemctl enable --now hyperliquid-profitability.timer
systemctl enable --now hyperliquid-wallet-research.timer
systemctl enable --now hyperliquid-selective-shadow.timer

# Run one selective cycle now. Path truth may remain BLOCKED while forward evidence
# accumulates; that is expected and is not a reason to fabricate a champion.
systemctl start hyperliquid-selective-shadow.service

printf '\n=== LOOP STATUS ===\n'
systemctl --no-pager --full status hyperliquid-shadow-validation.service || true
systemctl --no-pager --full status hlcopy-margin-snapshot.timer || true
systemctl --no-pager --full status hyperliquid-profitability.timer || true
systemctl --no-pager --full status hyperliquid-wallet-research.timer || true
systemctl --no-pager --full status hyperliquid-selective-shadow.timer || true

printf '\n=== SAFETY ===\n'
for unit in \
  hlcopy-margin-snapshot.service \
  hyperliquid-shadow-validation.service \
  hyperliquid-wallet-research.service \
  hyperliquid-profitability.service \
  hyperliquid-selective-shadow.service
do
  systemctl cat "$unit" | grep -q 'REAL_TRADING_ENABLED=NO'
  echo "$unit real_trading=OFF"
done

printf '\ncontinuous research + live-parity shadow installed\n'
