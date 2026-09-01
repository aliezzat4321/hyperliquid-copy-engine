#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (or with sudo): hl-ai-team-auth-codex" >&2
  exit 1
fi

USER_NAME=hl-codex-agent
AGENT_HOME=/var/lib/hyperliquid-ai-team/agents/codex/home
CODEX_HOME="$AGENT_HOME/.codex"
CODEX_BIN=/usr/local/bin/codex

id "$USER_NAME" >/dev/null 2>&1 || {
  echo "Missing $USER_NAME; install the Hyperliquid AI team first." >&2
  exit 1
}
[[ -x "$CODEX_BIN" ]] || { echo "Codex CLI missing: $CODEX_BIN" >&2; exit 1; }

install -d -o "$USER_NAME" -g "$USER_NAME" -m 0700 "$AGENT_HOME" "$CODEX_HOME"

cat <<'EOF'
This authenticates ONLY the isolated Hyperliquid Codex agent with your ChatGPT subscription.
Open the URL/code shown by Codex in your browser and finish the OpenAI login there.
Do not paste access/refresh tokens into GitHub, ChatGPT, or this terminal.
EOF

setpriv \
  --reuid="$(id -u "$USER_NAME")" \
  --regid="$(id -g "$USER_NAME")" \
  --init-groups \
  env HOME="$AGENT_HOME" CODEX_HOME="$CODEX_HOME" \
  "$CODEX_BIN" login --device-auth

chown -R "$USER_NAME:$USER_NAME" "$CODEX_HOME"
find "$CODEX_HOME" -type d -exec chmod 0700 {} +
find "$CODEX_HOME" -type f -exec chmod 0600 {} +

setpriv \
  --reuid="$(id -u "$USER_NAME")" \
  --regid="$(id -g "$USER_NAME")" \
  --init-groups \
  env HOME="$AGENT_HOME" CODEX_HOME="$CODEX_HOME" \
  "$CODEX_BIN" login status

echo 'CODEX_AGENT_AUTH=READY'
echo 'No credential material was copied to GitHub.'
