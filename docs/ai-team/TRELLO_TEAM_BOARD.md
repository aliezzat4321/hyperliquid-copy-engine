# Hyperliquid AI Team Trello board

Trello is a durable observability projection of the existing GitHub issue/PR and VM runtime ledger. It is not a task ledger, scheduler, source of accepted code, or second orchestrator. Losing a card or all Trello credentials must not interrupt engineering or research.

## Fixed board contract

- Board `Hyperliquid AI Team`: `6a9713c265a75ed50d4181d7`
- Backlog: `6a9713db6cfc74eee5b812b1`
- In Progress: `6a9713e3666e881387b18b9a`
- Review / CI: `6a9713fdfc33292cc90f5486`
- Blocked / Needs Decision: `6a9713f546dc3d1c3907c634`
- Done / Proven: `6a97140a2df53d4869073c91`
- Owner notification mention: `@aliezzat2`

The bridge maps `aliezzat4321/hyperliquid-copy-engine#<issue>` to exactly one card ID in its root-owned state file. Assignment and run start select In Progress; a PR, review, or CI event selects Review / CI; a blocker selects Blocked; merge/completion selects Done. An external GitHub issue/comment/PR/review/check event is normalized to the same event schema and therefore updates the same card immediately.

Every card description is replaced as one projection and shows priority, issue, PR/SHA, owner, reviewer/model, status, latest result, blocker, next action, elapsed time, ETA band, expected next checkpoint, and last update. Material results update the description rather than building an unbounded history.

## Event input

Invoke `scripts/trello_team_bridge.py` once per material transition and supply one JSON object on stdin (or `--event-file`). Required fields are `issue` and `event`; use `repository`, `title`, `priority`, `agent`, `task_type`, `model`, `owner`, `reviewer_model`, `status`, `pr`, `sha`, `result`, `blocker`, `next_action`, `task_started_at`, and `phase_started_at` when known. Production/data observation must supply a measured `eta_minutes`; the bridge will not invent a model ETA.

Events `BLOCKED`, `OWNER_ACTION`, review PASS/FAIL, CI FAIL, merged/completed, and significant result add one concise `@aliezzat2` comment. Starts, heartbeats, routine CI pending/pass, and repeated projections do not comment.

Runtime-ledger successful durations provide a p10–p90 band after five matching samples. Before that, conservative issue-specified fallbacks apply. Active work beyond the upper bound is marked `OVER_ETA`.

## Safety and recovery

Credentials live only at `/etc/hyperliquid-ai-team/trello.env`, mode `0600`, root-owned. They must never be copied into GitHub, cards, logs, prompts, agent homes, or model environments. The bridge emits no credential value. A missing/expired credential or Trello 429/5xx produces `TRELLO_SYNC=DEFERRED`, records a redacted failure, retries transient calls at most three times, and exits successfully so orchestration continues. The next material event retries convergence from canonical state.

`REAL_TRADING_ENABLED` remains disabled. This integration has no trading permission or order-routing role.

Rollback is to stop manager invocations and remove the Trello helper installation; GitHub and the VM ledger remain complete and authoritative.
