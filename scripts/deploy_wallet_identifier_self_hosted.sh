#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/root/hyperliquid-copy-engine}"
VOLUME_MOUNT="${HLCOPY_VOLUME_MOUNT:-/mnt/HC_Volume_106576526}"
STATE_DIR="/var/lib/hyperliquid-copy-engine/invo"
REGISTRY_PATH="${VOLUME_MOUNT}/hyperliquid/shadow/wallets.json"
INVO_ENV="/etc/hyperliquid-copy-engine/invo.env"

MINER_SERVICE="hyperliquid-invo-source-miner.service"
MINER_TIMER="hyperliquid-invo-source-miner.timer"
IDENTIFIER_SERVICE="hyperliquid-invo-wallet-identifier.service"
SYNC_SERVICE="hyperliquid-invo-verified-shadow-sync.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "deployment must run as root" >&2
  exit 1
fi

for required in "${REPO_DIR}/.git" "${REPO_DIR}/.venv/bin/python" "${INVO_ENV}"; do
  if [[ ! -e "${required}" ]]; then
    echo "required deployment dependency missing: ${required}" >&2
    exit 1
  fi
done

if ! mountpoint -q "${VOLUME_MOUNT}"; then
  echo "expected Hyperliquid data volume is not mounted: ${VOLUME_MOUNT}" >&2
  exit 1
fi

cd "${REPO_DIR}"
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "refusing deployment because ${REPO_DIR} has tracked local changes" >&2
  exit 1
fi

git fetch origin main
git checkout main
git merge --ff-only origin/main

if [[ ! -x "${REPO_DIR}/.venv/bin/python" ]]; then
  echo "virtualenv python is not executable after update" >&2
  exit 1
fi

install_rendered_unit() {
  local unit_name="$1"
  local source_path="${REPO_DIR}/deploy/systemd/${unit_name}"
  local target_path="/etc/systemd/system/${unit_name}"
  local temporary
  temporary="$(mktemp "/etc/systemd/system/.${unit_name}.XXXXXX")"
  sed "s|/root/hyperliquid-copy-engine|${REPO_DIR}|g" "${source_path}" >"${temporary}"
  chmod 0644 "${temporary}"
  mv -f "${temporary}" "${target_path}"
}

install_rendered_unit "${MINER_SERVICE}"
install -m 0644 \
  "${REPO_DIR}/deploy/systemd/${MINER_TIMER}" \
  "/etc/systemd/system/${MINER_TIMER}"
install_rendered_unit "${IDENTIFIER_SERVICE}"
install_rendered_unit "${SYNC_SERVICE}"

systemctl daemon-reload
systemd-analyze verify \
  "/etc/systemd/system/${MINER_SERVICE}" \
  "/etc/systemd/system/${IDENTIFIER_SERVICE}" \
  "/etc/systemd/system/${SYNC_SERVICE}"
systemctl enable --now "${MINER_TIMER}"

service_result() {
  systemctl show --property=Result --value "$1"
}

run_checked() {
  local unit="$1"
  local result
  echo "=== starting ${unit} ==="
  if ! systemctl start "${unit}"; then
    result="$(service_result "${unit}")"
    echo "${unit} start failed: ${result}" >&2
    systemctl status "${unit}" --no-pager -l >&2 || true
    journalctl -u "${unit}" -n 120 --no-pager >&2 || true
    exit 1
  fi
  result="$(service_result "${unit}")"
  if [[ "${result}" != "success" ]]; then
    echo "${unit} failed: ${result}" >&2
    systemctl status "${unit}" --no-pager -l >&2 || true
    journalctl -u "${unit}" -n 120 --no-pager >&2 || true
    exit 1
  fi
}

# First re-evaluate the already-mined queue under the newest resolver rule. This is
# the fastest path after a resolver upgrade and avoids unnecessary third-party API work.
run_checked "${IDENTIFIER_SERVICE}"
run_checked "${SYNC_SERVICE}"

need_refresh="$(${REPO_DIR}/.venv/bin/python - "${STATE_DIR}/identified_wallets.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    print("yes")
    raise SystemExit
names = {
    str(row.get("username") or "").strip().casefold()
    for row in payload.get("identities", [])
    if isinstance(row, dict)
}
print("no" if {"carmine", "bones"}.issubset(names) else "yes")
PY
)"

if [[ "${need_refresh}" == "yes" ]]; then
  # Pull fresh Invo evidence and let the normal OnSuccess chain run. Explicit
  # identifier/sync starts afterward are idempotent and ensure this deployment does
  # not return before the latest publication has been reconciled.
  run_checked "${MINER_SERVICE}"
  run_checked "${IDENTIFIER_SERVICE}"
  run_checked "${SYNC_SERVICE}"
fi

"${REPO_DIR}/.venv/bin/python" - \
  "${STATE_DIR}/identified_wallets.json" \
  "${STATE_DIR}/resolution_queue/resolution_queue.json" \
  "${STATE_DIR}/identifier_state.json" \
  "${REGISTRY_PATH}" <<'PY'
import json
import sys
from pathlib import Path


def load(path_text):
    path = Path(path_text)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None

identities = load(sys.argv[1]) or {}
queue = load(sys.argv[2]) or {}
state = load(sys.argv[3]) or {}
registry = load(sys.argv[4]) or {}

identity_rows = identities.get("identities", [])
queue_rows = queue.get("queue", [])
registry_rows = registry.get("wallets", [])

summary = {}
for target in ("carmine", "bones"):
    identity = next(
        (
            row
            for row in identity_rows
            if isinstance(row, dict)
            and str(row.get("username") or "").strip().casefold() == target
        ),
        None,
    )
    queued = next(
        (
            row
            for row in queue_rows
            if isinstance(row, dict)
            and str(row.get("username") or "").strip().casefold() == target
        ),
        None,
    )
    wallet = str(identity.get("wallet") or "").lower() if identity else ""
    shadow = next(
        (
            row
            for row in registry_rows
            if isinstance(row, dict)
            and wallet
            and str(row.get("source_ref") or "").lower() == wallet
        ),
        None,
    )
    summary[target] = {
        "verified": bool(identity),
        "wallet": wallet or None,
        "evidence_count": queued.get("evidence_count") if queued else None,
        "queue_status": queued.get("status") if queued else None,
        "shadow_stage": shadow.get("stage") if shadow else None,
        "shadow_enabled": shadow.get("enabled") if shadow else None,
    }

print("=== WALLET_IDENTIFIER_TARGET_STATUS ===")
print(json.dumps(summary, indent=2, sort_keys=True))
print("=== IDENTIFIER_STATE_PRESENT ===")
print(bool(state))
PY

systemctl list-timers --all --no-pager "${MINER_TIMER}"
echo "wallet identifier deployment completed"
