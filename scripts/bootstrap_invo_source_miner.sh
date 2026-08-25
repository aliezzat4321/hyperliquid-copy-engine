#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/root/hyperliquid-copy-engine}"
CONFIG_DIR="/etc/hyperliquid-copy-engine"
ENV_FILE="${CONFIG_DIR}/invo.env"
STATE_DIR="/var/lib/hyperliquid-copy-engine/invo"
SERVICE_NAME="hyperliquid-invo-source-miner.service"
TIMER_NAME="hyperliquid-invo-source-miner.timer"
IDENTIFIER_SERVICE_NAME="hyperliquid-invo-wallet-identifier.service"

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
env_backup=""
had_existing_env="no"
if [[ -f "${ENV_FILE}" ]]; then
  had_existing_env="yes"
  env_backup="${ENV_FILE}.backup.$$"
  install -m 0600 "${ENV_FILE}" "${env_backup}"
fi

rollback_environment() {
  if [[ "${had_existing_env}" == "yes" && -f "${env_backup}" ]]; then
    mv -f "${env_backup}" "${ENV_FILE}"
  elif [[ "${had_existing_env}" == "no" ]]; then
    rm -f "${ENV_FILE}"
  fi
}

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
install -m 0644 \
  "${REPO_DIR}/deploy/systemd/${IDENTIFIER_SERVICE_NAME}" \
  "/etc/systemd/system/${IDENTIFIER_SERVICE_NAME}"

systemctl daemon-reload

# Prove authentication/API compatibility before enabling unattended execution.
if ! systemctl start "${SERVICE_NAME}"; then
  rollback_environment
  echo "Initial Invo source-miner run failed; restored the previous credential." >&2
  journalctl -u "${SERVICE_NAME}" -n 40 --no-pager >&2
  exit 1
fi
service_result="$(systemctl show --property=Result --value "${SERVICE_NAME}")"
if [[ "${service_result}" != "success" ]]; then
  rollback_environment
  echo "Initial Invo source-miner run failed: ${service_result}" >&2
  journalctl -u "${SERVICE_NAME}" -n 40 --no-pager >&2
  exit 1
fi
journalctl -u "${IDENTIFIER_SERVICE_NAME}" -n 10 --no-pager || true
journalctl -u "${SERVICE_NAME}" -n 10 --no-pager

if ! systemctl start "${IDENTIFIER_SERVICE_NAME}"; then
  echo "Invo source collection succeeded, but wallet identification failed." >&2
  journalctl -u "${IDENTIFIER_SERVICE_NAME}" -n 40 --no-pager >&2
  exit 1
fi
identifier_result="$(systemctl show --property=Result --value "${IDENTIFIER_SERVICE_NAME}")"
if [[ "${identifier_result}" != "success" ]]; then
  echo "Initial Invo wallet-identifier run failed: ${identifier_result}" >&2
  journalctl -u "${IDENTIFIER_SERVICE_NAME}" -n 40 --no-pager >&2
  exit 1
fi

if [[ -n "${env_backup}" ]]; then
  rm -f "${env_backup}"
fi

systemctl enable --now "${TIMER_NAME}"
if ! systemctl is-active --quiet "${TIMER_NAME}"; then
  echo "Invo source-miner timer did not become active." >&2
  exit 1
fi
systemctl list-timers --all --no-pager "${TIMER_NAME}"

echo
echo "Invo source miner bootstrapped successfully."
echo "Secret: ${ENV_FILE} (mode 0600)"
echo "State:  ${STATE_DIR}"
echo "Timer:  ${TIMER_NAME}"
echo "Wallet identities: ${STATE_DIR}/identified_wallets.json"
