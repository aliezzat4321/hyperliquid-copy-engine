#!/usr/bin/env bash
set -euo pipefail

# One-time owner interaction for Anthropic Pro/Max unattended Claude Code use.
# The token is never written to the repository, command line, shell history, or logs.

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this helper as root on the Hyperliquid VM." >&2
  exit 1
fi

CLAUDE_HOME=/var/lib/hyperliquid-ai-team/agents/claude/home
SECRET_DIR=/etc/hyperliquid-ai-team
SECRET_FILE="$SECRET_DIR/claude.env"

id hl-claude-agent >/dev/null 2>&1 || {
  echo "hl-claude-agent is missing; install the AI-team toolchain first." >&2
  exit 1
}
command -v claude >/dev/null 2>&1 || {
  echo "Claude Code is not installed." >&2
  exit 1
}

install -d -m 0700 "$SECRET_DIR"
install -d -o hl-claude-agent -g hl-claude-agent -m 0700 "$CLAUDE_HOME" "$CLAUDE_HOME/.claude"

cat <<'EOF'
Anthropic account-owner step required.
Claude Code will now run its official `setup-token` flow. Complete the browser/account flow.
When it prints the long-lived OAuth token, copy it; this helper will then ask you to paste it
once with terminal echo disabled. Do not paste the token into GitHub, ChatGPT, or a repo file.
EOF

uid="$(id -u hl-claude-agent)"
gid="$(id -g hl-claude-agent)"
env HOME="$CLAUDE_HOME" CLAUDE_CONFIG_DIR="$CLAUDE_HOME/.claude" \
  setpriv --reuid="$uid" --regid="$gid" --init-groups \
  /usr/bin/claude setup-token

printf 'Paste the generated Claude setup token: ' >&2
IFS= read -r -s token
printf '\n' >&2
if [[ -z "$token" || "$token" == *$'\n'* ]]; then
  echo "Refusing empty/malformed token." >&2
  exit 1
fi

umask 077
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$token" > "$SECRET_FILE"
chmod 0600 "$SECRET_FILE"
unset token

echo "Claude unattended credential installed at $SECRET_FILE (root-only)."
echo "Run: hl-ai-team-status"
