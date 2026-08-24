#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/root/hyperliquid-copy-engine}"
CONFIG_DIR="/etc/hyperliquid-copy-engine"
ENV_FILE="${CONFIG_DIR}/invo.env"
STATE_DIR="/var/lib/hyperliquid-copy-engine/invo"
SERVICE_NAME="hyperliquid-invo-source-miner.service"
TIMER_NAME="hyperliquid-invo-source-miner.timer"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this bootstrap as root." >&2
  exit 1
fi

if [[ ! -d "${REPO_DIR}" ]]; then
  echo "Repository not found at ${REPO_DIR}" >&2
  exit 1
fi

if [[ ! -x "${REPO_DIR}/.venv/bin/python" ]]; then
  echo "Python virtualenv not found at ${REPO_DIR}/.venv" >&2
  exit 1
fi

install -d -m 0700 "${CONFIG_DIR}"
install -d -m 0700 "${STATE_DIR}"

refresh_token="${INVO_REFRESH_TOKEN:-}"
if [[ -z "${refresh_token}" ]]; then
  printf 'Paste the Invo refresh token (input hidden): ' >/dev/tty
  IFS= read -r -s refresh_token </dev/tty
  printf '\n' >/dev/tty
fi
refresh_token="${refresh_token#Bearer }"

if [[ -z "${refresh_token}" ]]; then
  echo "No refresh token supplied." >&2
  exit 1
fi

# JWT-like tokens should never require shell metacharacters. Reject unexpected input
# instead of attempting clever escaping into systemd EnvironmentFile syntax.
if [[ ! "${refresh_token}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "Refresh token contains unexpected characters; refusing to persist it." >&2
  exit 1
fi

umask 077
printf 'INVO_REFRESH_TOKEN=%s\n' "${refresh_token}" >"${ENV_FILE}.tmp"
chmod 0600 "${ENV_FILE}.tmp"
mv -f "${ENV_FILE}.tmp" "${ENV_FILE}"
unset refresh_token

install -m 0644 \
  "${REPO_DIR}/deploy/systemd/${SERVICE_NAME}" \
  "/etc/systemd/system/${SERVICE_NAME}"
install -m 0644 \
  "${REPO_DIR}/deploy/systemd/${TIMER_NAME}" \
  "/etc/systemd/system/${TIMER_NAME}"

systemctl daemon-reload

# Prove authentication and endpoint compatibility before enabling unattended runs.
systemctl start "${SERVICE_NAME}"
systemctl --no-pager --full status "${SERVICE_NAME}"

systemctl enable --now "${TIMER_NAME}"
systemctl --no-pager --full status "${TIMER_NAME}"
systemctl list-timers --all --no-pager "${TIMER_NAME}"

echo
echo "Invo source miner bootstrapped successfully."
echo "Secret: ${ENV_FILE} (mode 0600)"
echo "State:  ${STATE_DIR}"
echo "Timer:  ${TIMER_NAME}"
