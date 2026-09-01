#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPT=/opt/hyperliquid-ai-team/scripts
STATE=/var/lib/hyperliquid-ai-team/trello
ETC=/etc/hyperliquid-ai-team

[[ -f "$ROOT/AGENTS.md" ]] || { echo "not a Hyperliquid repo checkout" >&2; exit 1; }
git -C "$ROOT" remote get-url origin | grep -Eq 'github\.com[:/]aliezzat4321/hyperliquid-copy-engine(\.git)?$' || {
  echo "repository safety mismatch" >&2
  exit 1
}

echo 'TARGET_PROJECT=HYPERLIQUID_ONLY'
echo 'POLYMARKET_INSPECTION=NO'
echo 'POLYMARKET_MUTATION=NO'
echo 'REAL_TRADING_CHANGE=NO'

install -d -m 0755 "$OPT"
install -d -m 0700 "$STATE" "$ETC"
install -m 0755 "$ROOT/scripts/trello_team_bridge.py" "$OPT/trello_team_bridge.py"
install -m 0755 "$ROOT/scripts/trello_event_relay.py" "$OPT/trello_event_relay.py"
install -m 0755 "$ROOT/scripts/trello_vm_auth.py" "$OPT/trello_vm_auth.py"
install -m 0644 "$ROOT/deploy/systemd/hyperliquid-ai-team-trello-relay.service" /etc/systemd/system/hyperliquid-ai-team-trello-relay.service
install -m 0644 "$ROOT/deploy/systemd/hyperliquid-ai-team-trello-relay.path" /etc/systemd/system/hyperliquid-ai-team-trello-relay.path
install -m 0644 "$ROOT/deploy/systemd/hyperliquid-ai-team-trello-relay.timer" /etc/systemd/system/hyperliquid-ai-team-trello-relay.timer
ln -sfn "$OPT/trello_vm_auth.py" /usr/local/sbin/hl-ai-team-auth-trello

# Do not replay historical events into the newly attached board. Future material
# events are immediate via the path unit; timer is only a bounded retry fallback.
/usr/bin/python3 "$OPT/trello_event_relay.py" --initialize

systemctl daemon-reload
systemctl enable --now hyperliquid-ai-team-trello-relay.path >/dev/null
systemctl enable --now hyperliquid-ai-team-trello-relay.timer >/dev/null

if [[ -f "$ETC/trello.env" ]]; then
  test "$(stat -c '%a' "$ETC/trello.env")" = 600
  test "$(stat -c '%u' "$ETC/trello.env")" = 0
  echo 'TRELLO_VM_AUTH_FILE=READY'
else
  echo 'TRELLO_VM_AUTH_FILE=AUTH_REQUIRED'
fi

echo 'TRELLO_EVENT_RELAY=INSTALLED'
echo 'TRELLO_PRIMARY_SYNC=EVENT_DRIVEN_PATH_UNIT'
echo 'TRELLO_RETRY_FALLBACK=60_SECONDS'
echo 'POLYMARKET_TOUCHED=NO'
echo 'REAL_TRADING_TOUCHED=NO'
