# Protected manager wiring for the Trello bridge

This document is the implementation handoff for Issue #154. The autonomous builder intentionally did not modify installers, workflows, deployment, services, or credentials.

## Minimal protected changes

1. In `scripts/install_ai_team_orchestrator.sh`, add exactly one bridge-copy line after the existing orchestrator/runtime-ledger copies: `install -o root -g root -m 0755 "$ROOT/scripts/trello_team_bridge.py" "$OPT/scripts/trello_team_bridge.py"`. In the existing `colors` label map, add exactly `["ai-team:queued"]="C5DEF5"` so the queue-entry label is provisioned before the orchestrator polls it. These are the required protected installer deltas. Do not expose either credential environment variable to Codex or Claude units.
2. Run `trello_vm_auth.py` once from a root terminal. Confirm `/etc/hyperliquid-ai-team/trello.env` is owned by root and mode `0600`; READY means `/members/me`, the exact board, and all five exact lists passed.
3. The runtime ledger writes a root-only durable outbox entry after each issue-scoped material event. The orchestrator starts `trello_team_bridge.py --reconcile-dir /var/lib/hyperliquid-ai-team/trello-outbox --max-events 50` detached after checkpoint/cycle boundaries; it never waits for the bridge and model workers never run it.
4. Preserve the existing material events for assignment/model selection; build/research/review start; PR and target SHA; Claude PASS/FAIL and blockers; retry, rate limit, auth failure, CI state, merge/completion, and material result/next action. The bridge scans a bounded filename-ordered batch, retains failed events, skips all later events for an issue after its first failure in that pass, continues unrelated projections, and retries failures on later cycles.
5. In the existing GitHub event entry path, normalize issue/comment, PR, review, and check events into the identical schema and call the same function after GitHub state is persisted. Resolve PR events to their issue number using the existing assignment/ledger relationship. Never create a second mapping keyed by PR.
6. Treat exit status and `TRELLO_SYNC=DEFERRED` as observability only. Log only the issue/event and deferred reason class. Never fail, delay, or roll back the engineering transition because Trello failed. Missing local mappings must scan the visible cards on board `6a9713c265a75ed50d4181d7` and match only the title's issue token or canonical `Issue: #<issue>` field; PR/SHA references are not identity. Reuse the deterministic first exact match and create only when none exists.

## Operator queue contract

An issue enters the autonomous backlog only when it is open, trusted-owner-authored, labelled `ai-team:queued`, and contains both `AI_TEAM_AUTO_QUEUE=YES` and an integer `AI_TEAM_QUEUE_PRIORITY=<integer>` (lower runs first; issue number breaks ties). Optional `AI_TEAM_DEPENDS_ON=<issue numbers, comma-separated>` dependencies must each be closed or labelled `ai-team:done`. `ai-team:pending` means already assigned and is not a queue-entry label. Ready, active, previously attempted, blocked, closed, done, malformed, untrusted, and dependency-blocked issues are ineligible. With no due/active work and no ready issue, one eligible issue is promoted and claimed in the same cycle. A terminal block labels the issue and clears queued/ready/pending state so retry limits cannot restart it as a new BUILD.

The manager must obtain independent Claude Opus review of the exact current SHA after appending the protected installer and operator-documentation deltas. Do not use the routine asynchronous-review exception. VM smoke must verify the installed bridge is root-owned, `/etc/hyperliquid-ai-team/trello.env` is root/root `0600`, model units cannot read it, queue #120 is claimed after #154 is closed/done, and an idle cycle reports no dependency-satisfied explicit auto-queue candidate. Confirm `REAL_TRADING_ENABLED=NO`; no trading or unrelated integrations are part of this wiring.

Example manager payload:

```json
{"repository":"aliezzat4321/hyperliquid-copy-engine","issue":146,"event":"REVIEW_PASS","priority":"P0","title":"event-driven VM→Trello team board + ETA/notifications","task_type":"REVIEW","agent":"CLAUDE","model":"SONNET","owner":"CODEX_CHATGPT","reviewer_model":"CLAUDE / SONNET","status":"WAITING_CI","pr":123,"sha":"exact-reviewed-sha","result":"Claude PASS","next_action":"wait for exact-SHA CI","task_started_at":"2026-09-01T10:00:00Z","phase_started_at":"2026-09-01T10:08:00Z"}
```

## Acceptance drill after protected wiring

Use one synthetic issue and observe, without an hourly wait: assignment moves its sole card to In Progress; PR/review moves it to Review / CI; deliberate blocker moves it to Blocked and comments `@aliezzat2`; completion moves it to Done; an external GitHub comment and CI event update that same card. Remove or invalidate the credential during a synthetic transition and confirm the orchestrator proceeds while a redacted sync failure is recorded. Finally discard chat context and recover the task entirely from VM ledger, GitHub, and Trello.

No part of this wiring may enable real trading, alter capital/risk authorization, or bypass `LIVE_TRADING_GATE`.
