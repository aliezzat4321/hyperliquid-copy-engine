#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

REPO="aliezzat4321/hyperliquid-copy-engine"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE=/var/lib/hyperliquid-ai-team
OPT=/opt/hyperliquid-ai-team
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

# Parent directories permit traversal only; per-agent roots remain private and owned by
# their service identity. This lets an agent reach its own HOME without exposing the
# sibling agent's contents or the root-only orchestrator ledger/checkpoints.
install -d -m 0711 "$STATE"
install -d -m 0711 "$STATE/agents"
for spec in 'hl-codex-agent:codex' 'hl-claude-agent:claude'; do
  user="${spec%%:*}"
  agent="${spec##*:}"
  agent_root="$STATE/agents/$agent"
  home="$agent_root/home"
  if ! id "$user" >/dev/null 2>&1; then
    useradd --system --home-dir "$home" --create-home --shell /usr/sbin/nologin "$user"
  fi
  install -d -o "$user" -g "$user" -m 0710 "$agent_root"
  install -d -o "$user" -g "$user" -m 0700 "$home"
  install -d -o "$user" -g "$user" -m 0750 "$agent_root/worktrees"
  install -d -o "$user" -g "$user" -m 0750 "$agent_root/logs"
done
install -d -m 0700 "$STATE/orchestrator" "$STATE/events" "$STATE/runs" "$STATE/checkpoints"
install -d -m 0700 /run/hyperliquid-ai-team "$OPT/scripts" "$OPT/config" "$ETC"

# The standalone Codex CLI needs its matching Code Mode host plus the Linux
# workspace sandbox dependency. The helper pins matching official assets and fails closed
# on unsupported versions/architectures.
bash "$ROOT/scripts/install_codex_code_mode_host.sh"

# Preserve the isolated Codex identity once it exists. ChatGPT OAuth refresh tokens rotate;
# repeatedly copying root's auth.json can create two consumers of the same refresh token and
# invalidate the isolated agent. Root auth is therefore a first-install seed only. Subsequent
# deployments never overwrite the agent's own refreshed cache.
CODEX_HOME="$STATE/agents/codex/home/.codex"
install -d -o hl-codex-agent -g hl-codex-agent -m 0700 "$CODEX_HOME"
if [[ -f "$CODEX_HOME/auth.json" ]]; then
  chown hl-codex-agent:hl-codex-agent "$CODEX_HOME/auth.json"
  chmod 0600 "$CODEX_HOME/auth.json"
  echo 'CODEX_AUTH_CACHE=PRESERVED_EXISTING_AGENT'
elif [[ -f /root/.codex/auth.json ]]; then
  install -o hl-codex-agent -g hl-codex-agent -m 0600 /root/.codex/auth.json "$CODEX_HOME/auth.json"
  echo 'CODEX_AUTH_CACHE=SEEDED_FROM_ROOT_FIRST_INSTALL_ONLY'
else
  echo 'CODEX_AUTH_CACHE=AUTH_REQUIRED'
fi

install -m 0755 "$ROOT/scripts/ai_team_orchestrator.py" "$OPT/scripts/ai_team_orchestrator.py"
install -m 0644 "$ROOT/scripts/ai_team_runtime_ledger.py" "$OPT/scripts/ai_team_runtime_ledger.py"
install -m 0755 "$ROOT/scripts/ai_team_auth_codex.sh" "$OPT/scripts/ai_team_auth_codex.sh"
install -m 0755 "$ROOT/scripts/ai_team_auth_claude.sh" "$OPT/scripts/ai_team_auth_claude.sh"
install -m 0755 "$ROOT/scripts/install_codex_code_mode_host.sh" "$OPT/scripts/install_codex_code_mode_host.sh"
install -m 0644 "$ROOT/config/ai_team_router.json" "$OPT/config/ai_team_router.json"
install -m 0644 "$ROOT/config/ai_team_router.json" "$ETC/router.json"
install -m 0644 "$ROOT/deploy/systemd/hyperliquid-ai-team-orchestrator.service" /etc/systemd/system/hyperliquid-ai-team-orchestrator.service
install -m 0644 "$ROOT/deploy/systemd/hyperliquid-ai-team-orchestrator.timer" /etc/systemd/system/hyperliquid-ai-team-orchestrator.timer

cat > /usr/local/bin/hl-ai-team-status <<'EOF'
#!/usr/bin/env bash
exec /usr/bin/python3 /opt/hyperliquid-ai-team/scripts/ai_team_orchestrator.py --status "$@"
EOF
chmod 0755 /usr/local/bin/hl-ai-team-status
ln -sfn /usr/local/bin/hl-ai-team-status /usr/local/bin/hyperliquid-ai-status
ln -sfn "$OPT/scripts/ai_team_auth_codex.sh" /usr/local/sbin/hl-ai-team-auth-codex
ln -sfn "$OPT/scripts/ai_team_auth_claude.sh" /usr/local/sbin/hl-ai-team-auth-claude

/usr/bin/python3 "$OPT/scripts/ai_team_orchestrator.py" --init-db

# GitHub labels are durable coordination primitives. Create only our namespaced labels.
declare -A colors=(
  ["ai-team:ready"]="0E8A16"
  ["ai-team:pending"]="FBCA04"
  ["ai-team:running"]="1D76DB"
  ["ai-team:waiting-review"]="5319E7"
  ["ai-team:blocked"]="D73A4A"
  ["ai-team:done"]="006B75"
)
for label in "${!colors[@]}"; do
  encoded="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$label")"
  if ! gh api "repos/$REPO/labels/$encoded" >/dev/null 2>&1; then
    gh api --method POST "repos/$REPO/labels" \
      -f "name=$label" -f "color=${colors[$label]}" \
      -f "description=Hyperliquid autonomous AI-team orchestration" >/dev/null
  fi
done

systemctl daemon-reload
systemctl enable --now hyperliquid-ai-team-orchestrator.timer >/dev/null

echo 'AI_TEAM_INSTALL=OK'
echo 'RUNTIME_LEDGER_ROOT=/var/lib/hyperliquid-ai-team'
echo 'RUNTIME_STATUS_ISSUE=130'
echo 'TIMER_ENABLED_AND_ACTIVE=YES'
echo 'MODEL_IDLE_BEHAVIOR=NO_MODEL_CALLS_WITHOUT_DUE_TASK'
echo 'POLYMARKET_TOUCHED=NO'
echo 'REAL_TRADING_TOUCHED=NO'
