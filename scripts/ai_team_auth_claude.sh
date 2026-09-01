#!/usr/bin/env bash
set -euo pipefail

# One-time owner interaction for Anthropic Pro/Max Claude Code use on the
# dedicated reviewer identity. Use Claude's normal subscription OAuth login and
# let Claude manage/refresh its own Linux credential store. We deliberately do
# not rely on `claude setup-token`: that path has shown upstream 401 failures.

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this helper as root on the Hyperliquid VM." >&2
  exit 1
fi

CLAUDE_HOME=/var/lib/hyperliquid-ai-team/agents/claude/home
CLAUDE_CONFIG_DIR="$CLAUDE_HOME/.claude"
READY_FILE=/etc/hyperliquid-ai-team/claude.env

id hl-claude-agent >/dev/null 2>&1 || {
  echo "hl-claude-agent is missing; install the AI-team toolchain first." >&2
  exit 1
}
command -v claude >/dev/null 2>&1 || {
  echo "Claude Code is not installed." >&2
  exit 1
}

install -d -o hl-claude-agent -g hl-claude-agent -m 0700 \
  "$CLAUDE_HOME" "$CLAUDE_CONFIG_DIR" "$CLAUDE_HOME/.config" "$CLAUDE_HOME/.cache"
install -d -m 0700 /etc/hyperliquid-ai-team

# Remove any obsolete setup-token environment file so it cannot outrank the
# subscription login during authentication.
rm -f "$READY_FILE"

# Recover the small Claude state file if a previous interrupted login left only
# a backup. Otherwise start with an empty valid state file.
if [[ ! -f "$CLAUDE_HOME/.claude.json" ]]; then
  backup="$(find "$CLAUDE_CONFIG_DIR/backups" -maxdepth 1 -type f \
    -name '.claude.json.backup.*' -printf '%T@ %p\n' 2>/dev/null \
    | sort -nr | head -n1 | cut -d' ' -f2- || true)"
  if [[ -n "$backup" ]]; then
    install -o hl-claude-agent -g hl-claude-agent -m 0600 \
      "$backup" "$CLAUDE_HOME/.claude.json"
  else
    printf '{}\n' > "$CLAUDE_HOME/.claude.json"
    chown hl-claude-agent:hl-claude-agent "$CLAUDE_HOME/.claude.json"
    chmod 0600 "$CLAUDE_HOME/.claude.json"
  fi
fi

uid="$(id -u hl-claude-agent)"
gid="$(id -g hl-claude-agent)"
term_value="${TERM:-xterm-256color}"

claude_as_agent() {
  local -a command=("$@")
  setpriv --reuid="$uid" --regid="$gid" --init-groups \
    /usr/bin/env -i \
      HOME="$CLAUDE_HOME" \
      USER=hl-claude-agent \
      LOGNAME=hl-claude-agent \
      PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
      TERM="$term_value" \
      XDG_CONFIG_HOME="$CLAUDE_HOME/.config" \
      XDG_CACHE_HOME="$CLAUDE_HOME/.cache" \
      CLAUDE_CONFIG_DIR="$CLAUDE_CONFIG_DIR" \
      /bin/sh -c 'cd "$HOME" && exec "$@"' sh "${command[@]}"
}

# Fail closed if privilege dropping/environment isolation is not actually in
# effect. Never let Claude fall back to /root settings or credentials.
identity_check="$(
  setpriv --reuid="$uid" --regid="$gid" --init-groups \
    /usr/bin/env -i HOME="$CLAUDE_HOME" USER=hl-claude-agent LOGNAME=hl-claude-agent \
    /bin/sh -c 'printf "%s|%s|%s" "$(id -un)" "$HOME" "$LOGNAME"'
)"
if [[ "$identity_check" != "hl-claude-agent|$CLAUDE_HOME|hl-claude-agent" ]]; then
  echo "Refusing Claude login: isolated identity check failed." >&2
  exit 1
fi

echo "Claude login identity verified: hl-claude-agent (isolated home)."
cat <<'EOF'
Claude subscription login will open now under the isolated hl-claude-agent identity.
Choose your Claude.ai Pro/Max account and complete the browser flow.
SSH is supported: if the browser shows a login code, paste that code back into this terminal.
After Claude says Login successful, exit Claude (Ctrl+C is fine) so this helper can verify it.
EOF

claude_as_agent /usr/bin/claude || true

cred="$CLAUDE_CONFIG_DIR/.credentials.json"
if [[ ! -s "$cred" ]]; then
  echo "Claude subscription credential was not created at $cred." >&2
  echo "Run this helper again and complete the Claude.ai login before exiting." >&2
  exit 1
fi
chown hl-claude-agent:hl-claude-agent "$cred"
chmod 0600 "$cred"

# The current orchestrator accepts an EnvironmentFile as its readiness marker.
# Keep it comment-only so Claude falls through to its stored subscription OAuth
# credential; no OAuth token or API key is duplicated here.
umask 077
cat > "$READY_FILE" <<'EOF'
# Claude uses the hl-claude-agent subscription OAuth credential store.
EOF
chmod 0600 "$READY_FILE"

echo "Testing Claude Sonnet non-interactively..."
result="$(
  claude_as_agent /usr/bin/claude -p --model sonnet --output-format text \
    'Reply exactly: CLAUDE_AUTH_OK'
)"
printf '%s\n' "$result"
if [[ "$result" != *"CLAUDE_AUTH_OK"* ]]; then
  echo "Claude authentication verification failed." >&2
  exit 1
fi

echo "Claude subscription authentication installed for hl-claude-agent."
echo "Run: hl-ai-team-status"
