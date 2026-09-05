# Autonomous AI Team

Status: Hyperliquid-only engineering/review automation. This document does not authorize live trading.

## Typed remediation protocol

Review and CI failures are `BLOCKER_V1` records, not implicit requests for a code
change. The manager persists a canonical fingerprint and idempotent action key and
routes exactly one of `CODE_CHANGE`, `PR_METADATA`, `PROTECTED_ACTION`, `CI_RETRY`,
`REVIEW_RERUN`, `POLICY_RECONCILIATION`, or `TERMINAL`. Only `CODE_CHANGE` requires a
repository diff. Re-observing the same blocker changes its occurrence count but does
not create a child or spend an action attempt. Unknown, contradictory, and unauthorized
records fail closed for their dependency component.

Fresh high-value issues must supply the strict `AI_INITIAL_*` route authorized for
their `AI_TASK_CLASS` and begin as Claude Opus `RESEARCH`; successful research creates
a Codex `BUILD` child tied to the immutable research result. Explicit routine work
starts as Codex `BUILD`. Provider waits and scoped terminal blockers do not stop
unrelated dependency components.

## Architecture

GitHub remains the coordination record. A maintainer can mark an Issue `ai-team:ready`
for direct manual entry. For autonomous queue entry, an Issue must be open, authored
by the trusted owner, labelled `ai-team:queued`, and contain both
`AI_TEAM_AUTO_QUEUE=YES` and an integer `AI_TEAM_QUEUE_PRIORITY=<integer>`; lower
priority values run first and issue number breaks ties. An optional
`AI_TEAM_DEPENDS_ON=<issue numbers, comma-separated>` field delays promotion until
every dependency is closed or labelled `ai-team:done`.

When the ledger has no due or active work and no ready Issue exists, the orchestrator
promotes exactly one highest-priority eligible queued Issue and claims it in the same
cycle. Ready, active, previously attempted, blocked, closed, done, malformed,
untrusted, and dependency-blocked Issues are not eligible. `ai-team:pending` denotes
an already assigned Issue; it is not a queue-entry label. Queue eligibility is never
inferred from an Issue title. The VM timer wakes the orchestrator, not a model; the
orchestrator creates a machine-readable assignment, launches exactly one scoped agent
task, records the result, and exits.

```text
GitHub Issue ai-team:ready (direct) or eligible ai-team:queued (automatic promotion)
  -> root orchestrator + SQLite ledger + flock
  -> Codex isolated checkout (non-root, no GitHub credentials)
  -> root validates changes, commits/pushes, opens PR
  -> exact-SHA CLAUDE assignment comment
  -> Claude isolated checkout (Sonnet by default)
  -> PASS/FAIL exact-SHA result comment
     FAIL -> Codex repair -> delta-only Claude re-review
     PASS -> wait for CI
  -> routine + CI green -> root orchestrator squash-merges
  -> all agents exit; timer returns to GitHub-only polling
```

Runtime state is under `/var/lib/hyperliquid-ai-team`. Installed code is under `/opt/hyperliquid-ai-team`. The canonical application checkout `/root/hyperliquid-copy-engine` is not used as an agent working tree.

## Security boundaries

- Repository allowlist is exactly `aliezzat4321/hyperliquid-copy-engine`.
- The model processes run as `hl-codex-agent` and `hl-claude-agent`.
- Model users do not receive GitHub credentials. The root orchestrator owns push/comment/merge operations.
- Model processes run in transient systemd sandboxes with `ProtectHome=yes`, `/mnt` inaccessible, strict system protection, private `/tmp`, and only their checkout/home writable.
- Polymarket is out of scope. The orchestrator has no Polymarket path, unit, or repository allowlist.
- `REAL_TRADING_ENABLED` remains disabled. Autonomous changes that touch protected live-trading paths or introduce a live-enable value fail closed.
- Existing `LIVE_TRADING_GATE.md`, CI guards, exact-SHA review requirements, and owner authorization remain authoritative.
- No credential belongs in Git, Issue text, PR text, task logs, or model prompts.

## Machine protocol

Assignments are PR/Issue comments containing a hidden block beginning with `AI_TEAM_ASSIGNMENT_V1` and fields such as:

```text
AI_TEAM_PROTOCOL=1
ASSIGNMENT_ID=<durable id>
ASSIGNED_AGENT=CLAUDE
TASK_TYPE=REVIEW
MODEL_CLASS=SONNET
TASK_CLASS=ROUTINE
TARGET_PR=123
TARGET_SHA=<40-char sha>
STATUS=PENDING
```

Claude review results contain `AI_TEAM_RESULT_V1` with:

```text
REVIEWED_SHA=<40-char sha>
VERDICT=PASS|FAIL
REVIEWER=CLAUDE
MODEL_CLASS=SONNET|OPUS
BLOCKERS_JSON=[...]
```

A PR whose head changes during or after review is not mergeable from the old verdict. A fresh exact-SHA review is queued.

## Model routing

Policy lives in `config/ai_team_router.json`.

- `CODEX_DEFAULT`: build, repair, engineering, CI, deployment code, ordinary debugging.
- `SONNET`: routine and ordinary major-architecture independent PR review.
- `OPUS`: entry research for every high-value class; final review for quant/profitability,
  statistical methodology, unresolved disagreement, and capital-sensitive methodology.
  Major architecture uses Opus for final review only when the trusted Issue contains
  `OPUS_ESCALATION_REASON=MAJOR_ARCHITECTURE`.

Agents cannot choose Opus. The orchestrator validates the task class and escalation reason. Routine work with an Opus escalation request is rejected. Review-model routing is independent of merge eligibility: every recognized task class becomes merge-eligible only after an independent exact-SHA review PASS and green CI.

Automatic merge requires an explicit recognized task class (`TASK_CLASS` or `AI_TASK_CLASS`). A missing or invalid task class is recorded as `UNCLASSIFIED` and cannot be automatically merged. Protected AI-control-plane files additionally require a trusted Issue author, `AI_TEAM_PROTECTED_CHANGE=YES`, and membership in the narrow `AUTO_APPLY_CONTROL_PLANE_PATHS` allowlist before the same exact-SHA PASS + green-CI merge step. GitHub workflow, systemd deployment, trading/live, capital, credential, and other paths outside that allowlist remain non-automatically mergeable.

For an Issue that truly requires Opus, use an explicit trusted Issue field, for example:

```text
AI_TASK_CLASS=STATISTICAL_METHODOLOGY
OPUS_ESCALATION_REASON=STATISTICAL_METHODOLOGY
```

Do not add these fields to routine work.

## Context and token efficiency

Each normal invocation starts fresh. Codex is instructed to read `AGENTS.md`, `CURRENT_STATE.md`, the assigned Issue, latest trusted comments and only relevant/linked files. Claude receives the exact PR/SHA, base or previous-reviewed SHA, changed-file list, prior blockers, and only necessary adjacent context. Both first review and re-review explicitly forbid recursive or repository-wide rereads. Claude invocations have configured turn budgets (`REVIEW=12`, `RESEARCH=16`) in addition to wall-clock timeouts.

An interrupted task may resume by the vendor session/thread ID so rate-limit or timeout recovery does not discard work. A review failure is not resumed as an old conversation; it creates a fresh narrow repair/re-review cycle.

The SQLite ledger records task/model/run metadata and token usage when the CLI reports it. Idle timer polls do not invoke either model.

## Authentication

### Codex

The VM already uses supported ChatGPT Codex authentication. The dedicated Codex user receives a local copy of the Codex auth cache with mode `0600`; it receives no GitHub credentials. Headless device-code login is the fallback if the cache is ever invalid.

### Claude Pro/Max

Anthropic supports `claude setup-token` for unattended Claude Code use. The generated long-lived OAuth token must be created by the account owner. Run the installed helper on the VM:

```bash
hl-ai-team-auth-claude
```

It runs the official token flow and stores only `CLAUDE_CODE_OAUTH_TOKEN` in `/etc/hyperliquid-ai-team/claude.env` with root-only permissions. The token is never committed. If the token expires/revokes, rerun the helper.

## Service commands

```bash
hl-ai-team-status
systemctl status hyperliquid-ai-team-orchestrator.timer --no-pager
systemctl status hyperliquid-ai-team-orchestrator.service --no-pager
journalctl -u hyperliquid-ai-team-orchestrator.service -n 100 --no-pager
systemctl start hyperliquid-ai-team-orchestrator.service
```

`hl-ai-team-status` shows the current task, Codex/Claude state, model, PR/SHA, last verdict, blocker, rate-limit retry time, last successful run and recent failures.

## Disable / emergency stop

This stops new orchestration without altering application services or data:

```bash
systemctl disable --now hyperliquid-ai-team-orchestrator.timer
```

If an agent invocation is currently running, stop the one-shot service too:

```bash
systemctl stop hyperliquid-ai-team-orchestrator.service
```

Do not stop unrelated Hyperliquid services and do not touch Polymarket.

## Failure behavior

The scheduler watchdog persists component heartbeats, material-state fingerprints,
recovery actions, and alert incidents in the SQLite ledger. Process liveness alone is
not health: five identical material cycles are `STALLED`. Runnable work, expired
provider waits, worker timeouts, completed-but-unconsumed CI, closed PR assignments,
and a stale #130 mirror are recovered where safe and otherwise produce one operator
alert on open, blocker change, and recovery. The #130 projection reports queue age,
stale assignments, recovery actions, active alerts, productive progress, and
`HEALTHY`/`DEGRADED`/`STALLED` state.

- Agent crash: task remains in the ledger and retries with bounded attempts.
- VM/orchestrator restart: a `RUNNING` task becomes `RETRY`; workdir/session metadata is preserved.
- GitHub unavailable: cycle exits without a model call; the next timer cycle retries.
- Claude/Codex usage limit: checkpoint/session ID is retained and retry is deferred; no rapid retry loop.
- Stale SHA / PR changed while reviewing: old review becomes stale and a new exact-SHA review is queued.
- Two tasks: flock prevents concurrent orchestrator instances; the ledger selects one due task per cycle.
- Claude `FAIL`: blockers become a Codex repair task, followed by delta-only re-review.
- Missing/malformed model result: bounded retry, then `ai-team:blocked`.
- Missing Claude auth: review stops as `CLAUDE_AUTH_REQUIRED`; no fallback token hack.
- CI failure: no merge.
- Owner-sensitive/live path: no autonomous merge/change.

## Installation

The VM deployment copies only namespaced AI-team files and units:

```bash
scripts/install_ai_team_orchestrator.sh
```

The installer creates/updates only `hyperliquid-ai-team-orchestrator.*`, `/opt/hyperliquid-ai-team`, `/var/lib/hyperliquid-ai-team`, `/etc/hyperliquid-ai-team`, the two dedicated agent users, and `ai-team:*` GitHub labels.

## Adding another project later

Do not point this installation at a second repository. Create a separate project-specific configuration, state root, agent identities, systemd units, repository allowlist and safety policy. Reuse the protocol/code only after that project defines its own protected paths and authorization boundary. This prevents a future project from inheriting Hyperliquid credentials or permissions accidentally.
