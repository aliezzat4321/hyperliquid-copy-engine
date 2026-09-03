# Opus Diagnosis: Autonomous Repair-Loop Failures

Status: accepted diagnosis for Issue #172. This document is architecture only; it
does not authorize or implement a runtime change.

`LIVE-SENSITIVE: NO`

## Evidence and confidence

Verified from the accepted source and the Issue #172 transport evidence:

- `claim_ready_issue()` assigns every fresh issue to Codex `BUILD` with
  `CODEX_DEFAULT`; task class affects later review routing, not initial routing.
- `handle_review()` sends every machine-readable review `FAIL` to
  `enqueue_repair()`, which can create only a Codex `REPAIR` task.
- `handle_ci()` likewise treats every failed check as a code/test defect and sends it
  to Codex `REPAIR`.
- `validate_changes()` rejects an otherwise successful build or repair when the worktree
  has no file diff. `retry_or_block()` spends the generic task attempt count, and
  `block()` ultimately removes ready, pending, and queued labels.
- The #168 branch contains a build and a repair changing the orchestrator and tests.
  The repair added protected deploy-workflow validation, exact named-check gating, and
  live-sensitive classification. The #170 branch contains a build and two repairs; the
  repairs fix generated trusted-manager Python and then conservatively classify the PR
  live-sensitive. This is direct evidence that successive review findings drove new
  code commits through one undifferentiated repair route.
- #166 contains a narrow deploy-workflow test change. The supplied runtime record for
  #172 is `agent produced no file changes; max attempts reached`.
- Current project state records storage as P0 and full, with #93/#92/#91 downstream
  profitability work awaiting safe progress.

The exact GitHub review bodies, PR metadata snapshots, manager ledger rows, and deployed
SQLite task records for #168/#169/#170/#171/#166 were not available in this checkout.
Consequently, the attribution of a particular Sonnet sentence to a particular commit is
not independently verified here. The event ordering below is the narrowest inference
consistent with the branches and Issue #172's trusted failure history. The architecture
does not depend on guessing those missing sentences: future routing uses structured
blocker records and authoritative API state only.

## Failure-chain root cause

The root cause is a type error in the control protocol. A review or CI failure is stored
as prose and interpreted as “Codex must change files,” although failure is an observation,
not a remediation type. Actor, authority, expected side effect, and completion predicate
are absent. The child-task identity is only parent/type/SHA, attempts count executions
rather than unchanged blocker state, and the scheduler has a global claim-conflict path.
Together these turn non-code work into futile code jobs and let one control-plane chain
remove unrelated work from the runnable queue.

The observed sequence is:

1. #168 / PR #169 attempted autonomous protected-control-plane recovery. Sonnet found
   further deploy-workflow safety requirements. All findings were routed to Codex
   `REPAIR`; Codex added code/tests because no typed manager/protected-action route
   existed. This produced another SHA and another full review instead of completing a
   stable remediation record.
2. #170 / PR #171 broadened the trusted-manager bootstrap. One repair corrected the
   embedded workflow program and another changed PR live-sensitive metadata generation.
   The state machine again represented both as Codex file repairs. A metadata-only
   correction could not be completed by the manager without a repository diff, so an
   unchanged Codex run hit `agent produced no file changes` and re-entered the same
   generic retry circuit.
3. #166 supplied an earlier narrow workflow validation but had no typed recovery state
   with which #168/#170 could converge. Later attempts duplicated/superseded its purpose
   without a deterministic reconciliation transition.
4. On max attempts, `block()` removed all scheduling labels. Queue admission then treated
   the control-plane conflict as globally exclusive rather than dependency-scoped. #120
   storage could not resume even though diagnosis/control-plane maintenance itself did
   not make storage execution unsafe; #93/#92/#91 were consequently delayed behind the
   actual storage dependency.

The causal loop is therefore `FAIL -> untyped Codex REPAIR -> zero diff is an error ->
same evidence spends another attempt -> global BLOCKED`, not a lack of Codex persistence.

## Normative remediation table

Every failure producer must emit a `BLOCKER_V1` object with `class`, `source_kind`,
`source_id`, `subject_sha`, `rule_id`, and structured `observed` fields. Classification
must use this object plus GitHub/check/policy APIs; title and unconstrained prose are
never classifier inputs.

| Class | Deterministic input | Actor and idempotent action | Allowed next state | Terminal condition |
|---|---|---|---|---|
| `CODE_CHANGE` | Reviewer/check rule identifies repository paths or test failure with a reproducible command | Codex applies a diff on the existing branch | `WAITING_REVIEW` at new SHA | Safety violation or unchanged fingerprint after one completed code attempt |
| `PR_METADATA` | Explicit metadata field/rule plus current PR API value | Manager patches exactly the named title/body/label/base/draft field | `WAITING_CI` or `WAITING_REVIEW` at same SHA | API authorization denied or postcondition contradicts policy |
| `PROTECTED_ACTION` | Protected-path/policy rule and trusted-issue authorization object | Trusted manager executes the named allowlisted workflow/action | `WAITING_CI` or `WAITING_REVIEW` | Missing/expired authorization, preimage drift, or action outside allowlist |
| `CI_RETRY` | Named check is cancelled, timed out, stale, neutral when disallowed, runner-lost, or API-unavailable | Manager reruns the exact check suite for the same SHA | `WAITING_CI` | Bounded identical transient occurrences exhausted |
| `REVIEW_RERUN` | Review unavailable/incomplete, protocol-invalid, or stale SHA; no repository defect asserted | Manager requests the required reviewer/model for the exact current SHA | `WAITING_REVIEW` | Required model unavailable past the configured wait policy; provider limits remain waiting, not failure attempts |
| `POLICY_RECONCILIATION` | Machine policy marker, ledger/GitHub projection mismatch, or dependency state mismatch | Manager reconciles authoritative state and labels without changing product code | Previous safe checkpoint | Conflicting authoritative identities or an unsafe policy contradiction |
| `TERMINAL` | Enumerated safety, authorization, corrupt identity/state, or exhausted class-specific circuit breaker | Manager records evidence and blocks only the affected dependency component | `BLOCKED` | Immediate; no automatic actor is authorized |

Unknown, incomplete, contradictory, or multi-class blocker objects fail closed to
`TERMINAL` with reason `UNCLASSIFIED_BLOCKER`; they never default to Codex.

## State, progress, and retries

Persist one remediation row per blocker in the existing ledger:

```text
remediation_id, issue, pr, subject_sha, class, rule_id, source_kind, source_id,
observed_canonical_json, fingerprint, actor, action_key, status, occurrence_count,
action_attempts, last_action_at, completion_evidence, parent_assignment_id
```

`fingerprint = sha256(protocol_version + class + rule_id + source_kind + source_id +
subject_sha + canonical_json(observed))`. `action_key = sha256(fingerprint + actor +
canonical_json(requested_action))`. Canonical JSON has sorted keys and normalized GitHub
IDs/check names; prose summaries are excluded.

An event is progress only if at least one of these machine predicates changes:

- repository head SHA changes for `CODE_CHANGE`;
- the named metadata field reaches its expected value;
- the protected action records its expected postcondition and authorization ID;
- a new CI run ID is attached, or its status/conclusion changes;
- a new valid exact-SHA review result is attached;
- the authoritative ledger/GitHub projection mismatch is eliminated; or
- the blocker fingerprint disappears or changes.

Re-observing the same fingerprint increments `occurrence_count` for diagnostics but does
not increment `action_attempts`, create another child, or consume the task failure budget.
The unique `action_key` makes enqueue and execution idempotent across restarts. Only a
started action may increment its class-specific budget once. Budgets are: one Codex
attempt per unchanged `CODE_CHANGE` fingerprint; three distinct run IDs for `CI_RETRY`;
three manager API executions for metadata/reconciliation transient API failures; protected
actions follow their authorization object's bound; reviewer provider limits wait without
spending attempts. A completed action with no progress moves to `TERMINAL` rather than
looping. A changed fingerprint creates a new remediation row and budget.

`validate_changes()` applies only to tasks whose contract requires a repository diff.
Manager, CI, review, and reconciliation tasks declare `expected_effect=NO_REPO_DIFF` and
are completed solely by their structured postcondition.

## Queue isolation and recovery migration

Scheduling is dependency-component scoped. A task blocks only tasks whose explicit
`depends_on` closure contains it, or all tasks when an enumerated global safety invariant
(corrupt task identity, invalid live authorization boundary, or unavailable trusted
control plane required by every task) is active. Agent/model capacity is a resource pool,
not a dependency: an Opus wait does not prevent a safe Codex task from running.

On first deployment, the manager performs one idempotent migration:

1. Snapshot open PR heads, checks, machine review results, issue authorization, labels,
   and ledger ancestry for #166, #168, and #170.
2. Mark their legacy active/blocked assignments `SUPERSEDED_BY_REMEDIATION_V1`; do not
   delete history or spend attempts.
3. Reclassify each currently observable blocker from structured evidence. If evidence is
   absent or contradictory, create one `TERMINAL/UNCLASSIFIED_BLOCKER` on that issue only.
4. Reconcile #166 and #168 to their authoritative merged/open/closed state. Any still
   required work becomes typed remediation against the current PR SHA; obsolete work is
   closed as superseded.
5. Supersede #170 and PR #171 as specified below; do not salvage their mixed patch chain.
6. Remove the global queue conflict after no global safety invariant remains. Re-evaluate
   #120's explicit dependencies and automatically restore `queued`, then `ready`, if
   satisfied. Storage runs independently of issue-local remediation.
7. When #120's storage exit gate is satisfied, release #93 and #92 as Opus-first research
   tasks independently where their explicit dependencies permit. #91 may enter routine
   Codex implementation, but its profitability/sample-design gate creates an Opus review
   checkpoint. No chat intervention is part of these transitions.

## Opus-first entry route

Issue bodies gain exactly one machine block, parsed strictly rather than from title/prose:

```text
AI_TASK_CLASS=MAJOR_ARCHITECTURE
AI_INITIAL_ROUTE=RESEARCH
AI_INITIAL_AGENT=CLAUDE
AI_INITIAL_MODEL=OPUS
```

The allowlist maps `QUANT_PROFITABILITY`, `STATISTICAL_METHODOLOGY`,
`MAJOR_ARCHITECTURE`, `UNRESOLVED_DISAGREEMENT`, and
`CAPITAL_SENSITIVE_METHODOLOGY` to `RESEARCH/CLAUDE/OPUS`. Additional classes require an
explicit reviewed config allowlist entry. `ROUTINE` maps only to
`BUILD/CODEX_CHATGPT/CODEX_DEFAULT`. Missing fields are allowed only for `ROUTINE`, whose
safe default is Codex BUILD; missing class remains `UNCLASSIFIED` and cannot auto-merge.
Malformed, duplicate, contradictory, or unauthorized combinations create a scoped
`TERMINAL/INVALID_INITIAL_ROUTE` and never fall back to an agent.

Successful Opus research persists an architecture artifact/result and creates a Codex
BUILD child with its immutable result ID. Routine implementation receives Sonnet review.
Opus final review is required only by the high-stakes class/gate map. Research and build
occupy separate capacity lanes and obey explicit dependencies.

## Minimal implementation footprint

The later implementation is limited to:

- `config/ai_team_router.json`: blocker schema, initial-route allowlist, class budgets,
  and high-stakes final-review map.
- `scripts/ai_team_orchestrator.py`: `parse_task_class()` (replaced by strict route
  parsing), `claim_ready_issue()`, `handle_review()`, `handle_ci()`,
  `enqueue_repair()` (replaced by typed dispatch), `validate_changes()`,
  `retry_or_block()`, `block()`, `reconcile_handoffs()`, and the `Ledger` schema/accessors
  needed for remediation identity and migration.
- `scripts/ai_team_runtime_ledger.py` only if its projection must expose the new persisted
  remediation states.
- `tests/test_ai_team_orchestrator.py` and the narrow runtime-ledger test file if the
  projection changes.
- `docs/ai-team/AUTONOMOUS_TEAM.md`, `docs/ai-team/SYSTEM_MAP.md`, and durable state/
  decision docs only to describe the implemented contract.

Do not change workflows, trading/order/signing/key code, live systemd configuration,
storage implementation/policy, capital or risk authorization, Trello integration, or
profitability strategy code in that implementation. A protected workflow action remains
manager-owned and is not made Codex-writable.

## Acceptance-test matrix

| Case | Start/input | Expected result |
|---|---|---|
| Code repair | `CODE_CHANGE` with rule/path/reproducer | One Codex child; new SHA is progress; exact-SHA review follows |
| Zero-diff PR metadata E2E | Review emits `PR_METADATA` for missing required PR-body field at SHA S | No Codex task; manager PATCH uses one `action_key`; field postcondition completes at S; repeated fingerprint spends zero attempts; CI/review resumes and issue becomes non-blocked with no repository diff |
| Protected action | Valid trusted authorization and exact preimage | Trusted manager only; postcondition recorded; Codex cannot execute it |
| Missing protected authorization | `PROTECTED_ACTION` without valid authorization | Scoped terminal blocker; no mutation |
| Transient CI | Cancelled named check at S | Up to three distinct rerun IDs; duplicate event creates no rerun and spends no attempt |
| Deterministic CI failure | Reproducible test/rule includes paths and command | Classified `CODE_CHANGE`, not blind CI retry |
| Reviewer unavailable | Valid Opus assignment, provider limit | `WAITING_RATE_LIMIT`; no failure attempt; unrelated Codex work runs |
| Stale review | Result targets old SHA | One `REVIEW_RERUN` for current SHA; stale result cannot merge |
| State mismatch | Ledger pending but authoritative PR merged | `POLICY_RECONCILIATION` repairs projection without a diff |
| Unchanged blocker | Same canonical blocker observed repeatedly | One remediation/action; occurrence count changes, attempts do not |
| Changed blocker | Rule observation or SHA changes | New fingerprint and fresh bounded action |
| Major architecture entry | Valid `MAJOR_ARCHITECTURE/RESEARCH/CLAUDE/OPUS` | Initial assignment is Opus RESEARCH |
| Quant entry | Valid `QUANT_PROFITABILITY/RESEARCH/CLAUDE/OPUS` | Initial assignment is Opus RESEARCH |
| Routine entry | Valid `ROUTINE/BUILD/CODEX_CHATGPT/CODEX_DEFAULT` | Initial assignment is Codex BUILD |
| Bad entry metadata | Missing non-routine field, duplicate, malformed, contradiction, or class not allowlisted | Scoped `INVALID_INITIAL_ROUTE`; no agent assignment |
| Queue isolation | #170 scoped blocker and dependency-satisfied #120 | #120 becomes runnable; #170 consumes no unrelated lane slot |
| Migration restart | Migration interrupted after any step | Same keys resume without duplicate action; #166/#168 reconcile and #170 remains superseded |
| Live safety | Any route/action proposes enabling real trading or bypassing protected controls | Immediate terminal safety blocker; no mutation or merge |

## Safety argument

This architecture separates classification from authorization. A route grants an agent
work, never authority. Existing path scanning, exact-SHA review, green-CI merge gates,
trusted-author checks, protected preimages, and explicit user-issued live authorization
remain mandatory. Unknown blocker or route data fails closed. Manager metadata and
protected actions are allowlisted and postcondition-checked; they cannot be converted to
Codex file work. `REAL_TRADING_ENABLED` remains `NO`, and no task, migration, research
result, or remediation can create, infer, renew, or bypass live authorization.

## #170 disposition

Supersede #170. Its mixed build-and-repair chain embodies the untyped architecture being
replaced and should not be salvaged commit by commit. The migration records
`SUPERSEDED_BY=#172-remediation-v1`, closes its obsolete PR without merge, preserves its
evidence, and creates only the typed remediation rows still required by authoritative
current state. #168 and #166 are reconciled rather than automatically reopened; #120 is
released according to its own dependency and safety gates.

This single architecture prevents the repeated loop because a non-code blocker never
creates a Codex task, a zero diff is valid for actors whose postcondition is external,
and an unchanged fingerprint cannot spend another attempt. It also supplies an explicit
Opus-first entry route while keeping routine engineering Codex-first and queues isolated.
