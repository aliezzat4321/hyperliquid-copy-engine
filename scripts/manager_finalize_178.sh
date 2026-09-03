#!/usr/bin/env bash
set -euo pipefail

unset GITHUB_TOKEN GH_TOKEN
repo=aliezzat4321/hyperliquid-copy-engine
pr=180
issue=178
target=1db0ed66e682fde17a2c0a026008e97e62765426
deadline=$(( $(date +%s) + 7200 ))

while [[ $(date +%s) -lt $deadline ]]; do
  pr_json="$(gh api "repos/$repo/pulls/$pr")"
  state="$(jq -r .state <<<"$pr_json")"
  merged="$(jq -r .merged <<<"$pr_json")"
  head="$(jq -r .head.sha <<<"$pr_json")"

  if [[ "$merged" == true ]]; then
    echo 'FINALIZER=ALREADY_MERGED'
    exit 0
  fi
  [[ "$state" == open ]] || { echo "FINALIZER=STOP_PR_STATE_$state"; exit 20; }
  [[ "$head" == "$target" ]] || { echo "FINALIZER=STOP_HEAD_MOVED_$head"; exit 21; }

  issue_json="$(gh api "repos/$repo/issues/$issue")"
  assoc="$(jq -r .author_association <<<"$issue_json")"
  body="$(jq -r .body <<<"$issue_json")"
  [[ "$assoc" =~ ^(OWNER|MEMBER|COLLABORATOR)$ ]] || { echo 'FINALIZER=STOP_UNTRUSTED_ISSUE'; exit 22; }
  grep -q '^AI_TEAM_PROTECTED_CHANGE=YES$' <<<"$body" || { echo 'FINALIZER=STOP_MISSING_PROTECTED_AUTH'; exit 23; }

  runs="$(gh api "repos/$repo/actions/runs?head_sha=$target&per_page=100")"
  ci_ok="$(jq -r '["ci","ai-team-contract","live-sensitive-guard"] as $req | [$req[] as $n | ([.workflow_runs[] | select(.name==$n)] | sort_by(.created_at) | last | .conclusion)] | if length==3 and all(.[]; .=="success") then "YES" else "NO" end' <<<"$runs")"

  comments="$(gh api --paginate "repos/$repo/issues/$pr/comments?per_page=100" | jq -s 'add')"
  latest_body="$(jq -r --arg t "$target" '[.[] | select(((.body // "") | contains("AI_TEAM_RESULT_V1")) and ((.body // "") | contains("REVIEWED_SHA=" + $t)) and ((.body // "") | contains("MODEL_CLASS=OPUS")))] | sort_by(.created_at) | last | .body // ""' <<<"$comments")"
  verdict="$(grep -oE '^VERDICT=(PASS|FAIL)$' <<<"$latest_body" | tail -1 | cut -d= -f2 || true)"
  [[ -n "$verdict" ]] || verdict=NONE

  echo "FINALIZER_CHECK ci=$ci_ok opus=$verdict head=$head"
  if [[ "$verdict" == FAIL ]]; then
    echo 'FINALIZER=STOP_OPUS_FAIL'
    exit 30
  fi
  if [[ "$ci_ok" == YES && "$verdict" == PASS ]]; then
    gh pr merge "$pr" --repo "$repo" --merge --match-head-commit "$target"
    echo 'FINALIZER=MERGED_EXACT_REVIEWED_SHA'
    exit 0
  fi
  sleep 120
done

echo 'FINALIZER=TIMEOUT_NO_MUTATION'
exit 40
