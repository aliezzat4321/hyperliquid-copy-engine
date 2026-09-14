#!/usr/bin/env bash
set -euo pipefail

REPO='aliezzat4321/hyperliquid-copy-engine'
AGENT_USER='hl-codex-agent'
AGENT_HOME='/var/lib/hyperliquid-ai-team/agents/codex/home'
REVIEW_ROOT='/var/lib/hyperliquid-ai-team/agents/codex/direct-reviews'
SUDO=''
if [ "$(id -u)" -ne 0 ]; then SUDO='sudo'; fi

$SUDO test -x /usr/local/bin/codex
$SUDO test -x /usr/local/bin/codex-code-mode-host
$SUDO test -x /usr/local/bin/bwrap
$SUDO install -d -m 0750 -o "$AGENT_USER" -g "$AGENT_USER" "$REVIEW_ROOT"

overall_rc=0

review_one() {
  local issue="$1"
  local sha="$2"
  local prompt_path="$3"
  local work="$REVIEW_ROOT/${issue}-${GITHUB_RUN_ID:-manual}"
  local output="${RUNNER_TEMP:-/tmp}/codex-review-${issue}.jsonl"
  local result="${RUNNER_TEMP:-/tmp}/codex-review-${issue}.txt"

  $SUDO rm -rf "$work"
  git clone --quiet "https://github.com/${REPO}.git" "$work"
  git -C "$work" checkout --quiet --detach "$sha"
  if [ "$(git -C "$work" rev-parse HEAD)" != "$sha" ]; then
    echo "DIRECT_CODEX_REVIEW_SETUP_FAIL issue=$issue reason=stale_checkout"
    overall_rc=1
    return
  fi
  $SUDO chown -R "$AGENT_USER:$AGENT_USER" "$work"

  set +e
  cat "$prompt_path" | $SUDO systemd-run \
    --pipe --wait --collect --quiet \
    --unit="hl-ai-codex-direct-${issue}-${GITHUB_RUN_ID:-manual}" \
    --uid="$AGENT_USER" --gid="$AGENT_USER" \
    --working-directory="$work" \
    --property=NoNewPrivileges=yes \
    --property=PrivateTmp=yes \
    --property=ProtectHome=yes \
    --property=ProtectSystem=strict \
    --property=RestrictSUIDSGID=yes \
    --property=InaccessiblePaths=/mnt \
    --property="ReadWritePaths=$work $AGENT_HOME" \
    --setenv="HOME=$AGENT_HOME" \
    --setenv="CODEX_HOME=$AGENT_HOME/.codex" \
    /usr/local/bin/codex exec --json --sandbox workspace-write --skip-git-repo-check - \
    > "$output" 2>&1
  local rc=$?
  set -e

  if [ "$rc" -ne 0 ]; then
    echo "DIRECT_CODEX_REVIEW_RUNTIME_FAIL issue=$issue rc=$rc"
    tail -n 40 "$output" || true
    overall_rc=1
    return
  fi

  if ! python3 - "$output" "$result" "$sha" <<'PY'
import json, re, sys
src, dst, expected = sys.argv[1:]
message = ''
for raw in open(src, encoding='utf-8', errors='replace'):
    raw = raw.strip()
    if not raw.startswith('{'):
        continue
    try:
        row = json.loads(raw)
    except json.JSONDecodeError:
        continue
    item = row.get('item') or {}
    if item.get('type') in {'agent_message', 'message'} and isinstance(item.get('text'), str):
        message = item['text']
    if row.get('type') in {'message.completed', 'response.completed'}:
        candidate = row.get('message') or row.get('response') or {}
        if isinstance(candidate, dict) and isinstance(candidate.get('text'), str):
            message = candidate['text']
if not message:
    raise SystemExit('no Codex reviewer message found')
sha = re.search(r'(?mi)^REVIEWED_SHA=([0-9a-f]{40})\s*$', message)
verdict = re.search(r'(?mi)^VERDICT=(PASS|FAIL)\s*$', message)
blockers = re.search(r'(?mi)^BLOCKERS_JSON=(\[.*\])\s*$', message)
if not sha or sha.group(1) != expected:
    raise SystemExit('invalid or stale REVIEWED_SHA')
if not verdict or not blockers:
    raise SystemExit('missing VERDICT/BLOCKERS_JSON')
parsed = json.loads(blockers.group(1))
if not isinstance(parsed, list):
    raise SystemExit('BLOCKERS_JSON is not a list')
if verdict.group(1) == 'PASS' and parsed:
    raise SystemExit('PASS cannot carry blockers')
if verdict.group(1) == 'FAIL' and not parsed:
    raise SystemExit('FAIL must carry blockers')
open(dst, 'w', encoding='utf-8').write(message.strip() + '\n')
PY
  then
    echo "DIRECT_CODEX_REVIEW_PARSE_FAIL issue=$issue"
    tail -n 40 "$output" || true
    overall_rc=1
    return
  fi

  if ! $SUDO -u "$AGENT_USER" git -C "$work" diff --quiet -- .; then
    echo "DIRECT_CODEX_REVIEW_INVALID issue=$issue reason=tracked_source_modified"
    $SUDO -u "$AGENT_USER" git -C "$work" diff --stat -- . || true
    overall_rc=1
    return
  fi

  echo "===== DIRECT_CODEX_REVIEW_BEGIN issue=$issue sha=$sha ====="
  cat "$result"
  echo "===== DIRECT_CODEX_REVIEW_END issue=$issue sha=$sha ====="
}

review_one \
  331 \
  8ae1cfb068291fd8a65ccb0070251e64d97401eb \
  "$GITHUB_WORKSPACE/.github/review-prompts/lane3-331.txt"
review_one \
  333 \
  54b040ff4419fa3eb334bbabc293899a0f87c901 \
  "$GITHUB_WORKSPACE/.github/review-prompts/lane3-333.txt"

exit "$overall_rc"
