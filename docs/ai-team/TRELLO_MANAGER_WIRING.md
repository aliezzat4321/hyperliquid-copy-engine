# Protected manager wiring for the Trello bridge

This document is the implementation handoff for an owner-authorized change. The autonomous builder intentionally did not modify `scripts/ai_team_*`, workflows, deployment, installers, services, or credentials.

## Minimal protected changes

1. Install `scripts/trello_team_bridge.py` as a root-owned executable available only to the existing manager. Install `scripts/trello_vm_auth.py` for an operator to run interactively as root. Do not expose either credential environment variable to Codex or Claude units.
2. Run `trello_vm_auth.py` once from a root terminal. Confirm `/etc/hyperliquid-ai-team/trello.env` is owned by root and mode `0600`; READY means `/members/me`, the exact board, and all five exact lists passed.
3. Add one small manager-side function that serializes a normalized event to the bridge's stdin. Run it synchronously immediately after the existing ledger/GitHub transition is durable. Do not add a polling loop, queue consumer, scheduler, or Trello-originated command path.
4. Call that function at the existing transition sites for assignment/model selection; build/research/review start; PR and target SHA; Claude PASS/FAIL and blockers; retry, rate limit, auth failure, or other blocker; CI state; merge/completion; and material result/next action. Use the ledger timestamps as `task_started_at` and `phase_started_at`.
5. In the existing GitHub event entry path, normalize issue/comment, PR, review, and check events into the identical schema and call the same function after GitHub state is persisted. Resolve PR events to their issue number using the existing assignment/ledger relationship. Never create a second mapping keyed by PR.
6. Treat exit status and `TRELLO_SYNC=DEFERRED` as observability only. Log only the issue/event and deferred reason class. Never fail, delay, retry, or roll back the engineering transition because Trello failed.

Example manager payload:

```json
{"repository":"aliezzat4321/hyperliquid-copy-engine","issue":146,"event":"REVIEW_PASS","priority":"P0","title":"event-driven VM→Trello team board + ETA/notifications","task_type":"REVIEW","agent":"CLAUDE","model":"SONNET","owner":"CODEX_CHATGPT","reviewer_model":"CLAUDE / SONNET","status":"WAITING_CI","pr":123,"sha":"exact-reviewed-sha","result":"Claude PASS","next_action":"wait for exact-SHA CI","task_started_at":"2026-09-01T10:00:00Z","phase_started_at":"2026-09-01T10:08:00Z"}
```

## Acceptance drill after protected wiring

Use one synthetic issue and observe, without an hourly wait: assignment moves its sole card to In Progress; PR/review moves it to Review / CI; deliberate blocker moves it to Blocked and comments `@aliezzat2`; completion moves it to Done; an external GitHub comment and CI event update that same card. Remove or invalidate the credential during a synthetic transition and confirm the orchestrator proceeds while a redacted sync failure is recorded. Finally discard chat context and recover the task entirely from VM ledger, GitHub, and Trello.

No part of this wiring may enable real trading, alter capital/risk authorization, or bypass `LIVE_TRADING_GATE`.
