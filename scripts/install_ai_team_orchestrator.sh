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

for spec in 'hl-codex-agent:codex' 'hl-claude-agent:claude'; do
  user="${spec%%:*}"
  agent="${spec##*:}"
  home="$STATE/agents/$agent/home"
  if ! id "$user" >/dev/null 2>&1; then
    useradd --system --home-dir "$home" --create-home --shell /usr/sbin/nologin "$user"
  fi
  install -d -o "$user" -g "$user" -m 0700 "$home"
  install -d -o "$user" -g "$user" -m 0750 "$STATE/agents/$agent/worktrees"
  install -d -o "$user" -g "$user" -m 0750 "$STATE/agents/$agent/logs"
done
install -d -m 0700 "$STATE/orchestrator" /run/hyperliquid-ai-team "$OPT/scripts" "$OPT/config" "$ETC"

install -m 0755 "$ROOT/scripts/ai_team_orchestrator.py" "$OPT/scripts/ai_team_orchestrator.py"
install -m 0755 "$ROOT/scripts/ai_team_auth_claude.sh" "$OPT/scripts/ai_team_auth_claude.sh"
install -m 0644 "$ROOT/config/ai_team_router.json" "$OPT/config/ai_team_router.json"
install -m 0644 "$ROOT/config/ai_team_router.json" "$ETC/router.json"
install -m 0644 "$ROOT/deploy/systemd/hyperliquid-ai-team-orchestrator.service" /etc/systemd/system/hyperliquid-ai-team-orchestrator.service
install -m 0644 "$ROOT/deploy/systemd/hyperliquid-ai-team-orchestrator.timer" /etc/systemd/system/hyperliquid-ai-team-orchestrator.timer

cat > /usr/local/bin/hl-ai-team-status <<'EOF'
#!/usr/bin/env bash
exec /usr/bin/python3 /opt/hyperliquid-ai-team/scripts/ai_team_orchestrator.py --status "$@"
EOF
chmod 0755 /usr/local/bin/hl-ai-team-status
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
systemctl enable hyperliquid-ai-team-orchestrator.timer >/dev/null

echo 'AI_TEAM_INSTALL=OK'
echo 'TIMER_ENABLED=YES'
echo 'MODEL_IDLE_BEHAVIOR=NO_MODEL_CALLS_WITHOUT_DUE_TASK'
echo 'POLYMARKET_TOUCHED=NO'
echo 'REAL_TRADING_TOUCHED=NO'
