# Claude Opus Repair-Loop Diagnosis Request

`LIVE-SENSITIVE: NO`

This transport-only documentation change does not alter live permissions,
trading code, order routing, signing or key handling, systemd configuration, or
safety thresholds.

## Purpose and execution instruction

This document is the transport artifact for GitHub Issue #172. The ensuing
**Claude Opus REVIEW invocation is itself the requested diagnosis execution**.
Opus must not merely review this document for completeness or judge whether the
request is well formed. Before emitting its final machine-readable review lines,
Opus must provide the complete root-cause diagnosis and prescribe exactly one
minimal, testable recovery architecture meeting every requirement below.

This is diagnosis only. Do not implement the architecture, modify the transport
artifact, or propose a follow-up repair commit as the result of this review. A
transport PR is not completion of Issue #172. The issue remains open until the
actual Opus diagnosis is durably captured.

The review body is the deliverable, not commentary about whether this request is
adequate. A review that contains only `VERDICT`, prompt-quality feedback, or a
request for Codex to change repository files has not executed Issue #172. Even
when some historical evidence is unavailable in the review context, Opus must
complete the architecture from verified evidence, label each evidence gap and
resulting inference, and reserve `VERDICT=FAIL` for a genuine safety or evidence
blocker that makes a single recovery architecture impossible to prescribe.

## Safety and scope constraints

- Repository: `aliezzat4321/hyperliquid-copy-engine` only.
- `REAL_TRADING_ENABLED` remains `NO`. Do not enable real/live trading, add or
  use trading keys, place orders, change capital or risk authorization, or bypass
  `LIVE_TRADING_GATE` or protected-path controls.
- Do not access or discuss Polymarket.
- Do not implement or edit workflows, orchestrator/runtime code, tests, config,
  Trello code, trading, storage, credentials, capital controls, or any other
  control-plane implementation as part of this diagnosis.
- If the only blocker is GitHub or PR metadata, prescribe routing to the
  manager/GitHub layer for direct metadata repair. Do not route a metadata-only
  remediation to Codex.

## Evidence and failure chain to diagnose

Treat the Issue #172 body, trusted comments, Sonnet blockers and repair chains on
PRs #169 and #171, and the current repository/runtime observations as evidence.
Inspect the exact artifacts where available rather than inferring causes from
issue titles or free-form prose.

The current accepted orchestrator contains these relevant mechanics:

- `claim_ready_issue()` creates every fresh task as `TASK_TYPE=BUILD`,
  `AGENT=CODEX_CHATGPT`, `MODEL_CLASS=CODEX_DEFAULT`, even though
  `parse_task_class()` recognizes high-value task classes and `route_review()`
  can select Opus later.
- `handle_review()` maps every Claude `VERDICT=FAIL` to
  `enqueue_repair(...)`, which always creates a Codex `REPAIR` task.
- `handle_ci()` classifies every CI failure as a repairable code/test defect and
  sends it to the same Codex `REPAIR` route.
- `validate_changes()` raises `agent produced no file changes` whenever a Codex
  build or repair produces no repository diff.
- `retry_or_block()` consumes the generic attempt budget and ultimately marks
  the task `BLOCKED`; `block()` removes ready, pending, and queued labels.
- Recovery and handoff reconciliation are organized around BUILD/REPAIR to
  REVIEW and failed REVIEW to REPAIR. The deployed runtime cannot directly start
  the required Opus RESEARCH/ARCHITECT task for this issue.
- The supplied Issue #172 runtime record reports
  `Codex postprocess/finalize failed: agent produced no file changes; max attempts reached`.
- The histories for #168/#170 and PRs #169/#171 show repeated implementation and
  repair cycles. Diagnose the specific Sonnet blockers and distinguish code
  defects from PR-metadata mutations, trusted-manager actions, protected-path
  actions, CI reruns/transient failures, review reruns, and policy/state
  reconciliation.
- Explain why this control-plane sequence was allowed to stall #120 storage and,
  transitively, the #93/#92/#91 profitability lanes instead of isolating
  unrelated work that could proceed safely.

Do not accept the bullets above as a sufficient diagnosis. Reconstruct the
causal sequence across #168, PR #169, #170, and PR #171; identify which actor and
state transition was wrong at each repeated failure; and explain why the current
attempt, idempotency, queue, and recovery semantics did not make progress.

## Required single architecture

Return one concrete architecture, not alternatives or a menu of speculative
options. It must include all of the following:

1. A small finite set of blocker/remediation classes.
2. Deterministic classification inputs, machine-readable wherever possible,
   explicitly avoiding title or free-form prose inference where that would be
   unsafe.
3. The correct actor and action for every class, including Codex code repair,
   manager PR-metadata mutation, trusted-manager protected action, CI retry,
   reviewer rerun, and a genuine terminal blocker.
4. Exact retry and idempotency semantics. Define a machine-checkable progress
   identity/fingerprint and state transition rules so an unchanged blocker cannot
   consume attempts repeatedly without state progress.
5. Queue-isolation semantics under which control-plane maintenance does not block
   unrelated storage or profitability work unless it truly prevents safe
   execution.
6. A deterministic migration/recovery procedure for currently blocked #170,
   #168, and #166, plus automatic resumption of #120 without chat or manual
   babysitting. State dependencies and resulting queue states explicitly.
7. Minimal implementation scope: name the exact files and functions that would
   change in the later implementation, and name the files/subsystems that must
   not change.
8. Acceptance tests for every routing and retry invariant, including at least one
   end-to-end PR-metadata-blocker case in which **zero code changes are expected**
   and the system nevertheless repairs the metadata and recovers automatically.
9. A fail-closed safety argument preserving `REAL_TRADING_ENABLED=NO`, explicit
   authorization boundaries, and protected-path controls.
10. A definitive recommendation to supersede, salvage, or close #170 after this
    diagnosis, with the reason and exact transition.
11. The Opus-first task-entry design specified below as part of the same
    architecture, not as a separate subsystem.

For clarity, present the finite classes and actor/action transitions in one
normative table, then specify the state machine, persistence/idempotency keys,
migration, minimal code footprint, and acceptance-test matrix precisely enough
for a subsequent Codex implementation issue to be mechanical rather than
architectural.

## Opus-first task-entry requirements

The architecture must add an explicit, machine-readable initial task route (for
example an initial `RESEARCH` or `BUILD` field) and select the initial agent/model
from that route, the explicit task class, and allowed escalation policy. It must
not infer the route from issue title or prose.

The following classes must be able to start directly as Claude Opus
RESEARCH/ARCHITECT before Codex implementation:

- `QUANT_PROFITABILITY`
- `STATISTICAL_METHODOLOGY`
- `MAJOR_ARCHITECTURE`
- `UNRESOLVED_DISAGREEMENT`
- `CAPITAL_SENSITIVE_METHODOLOGY`
- any other class explicitly approved through the same fail-closed policy

Routine engineering must remain Codex-first. The default high-ROI sequence is:
Opus research/architecture, then Codex implementation, then Sonnet routine
review, with Opus final review only when the task class or a high-stakes gate
requires it. Opus capacity should be reserved for high-value decisions rather
than routine coding.

Apply this routing to the post-storage profitability lanes: #93 and #92 begin
with Opus architecture/research; #91 may use routine implementation where
appropriate, but its profitability/sample-design gate escalates to Opus. Later
direct profitability or strategy-selection work is Opus-first. An Opus-first
research task must not monopolize the queue or stop unrelated safe Codex work
whose dependencies are satisfied.

Acceptance tests must prove that fresh ready `MAJOR_ARCHITECTURE` and
`QUANT_PROFITABILITY` issues are initially assigned to Claude Opus RESEARCH,
while a fresh ready `ROUTINE` issue is initially assigned to Codex BUILD. Include
fail-closed cases for absent, malformed, contradictory, or unauthorized initial
route metadata.

## Required diagnosis deliverable and durable follow-up

The Opus review must use the following ordered output contract. Every section is
mandatory and must contain the diagnosis itself rather than instructions for a
future agent:

1. `Evidence and confidence`: verified facts, unavailable evidence, and clearly
   labelled inferences.
2. `Failure-chain root cause`: an event-by-event mapping for issue #168 / PR
   #169 and issue #170 / PR #171, plus the relationship to #166 and the stalled
   #120 queue path.
3. `Normative remediation table`: the finite blocker classes, deterministic
   inputs, actor, action, allowed next states, and terminal condition.
4. `State, progress, and retries`: persisted state, blocker fingerprint,
   idempotency key, progress predicate, retry budgets, and behavior for an
   unchanged fingerprint.
5. `Queue isolation and recovery migration`: dependency-scoped scheduling and
   exact automatic transitions for #166, #168, #170, #120, then #93/#92/#91.
6. `Opus-first entry route`: explicit metadata schema, allowlist and fail-closed
   validation, task-class routes, implementation handoff, and review escalation.
7. `Minimal implementation footprint`: exact later files/functions that may
   change and explicit exclusions.
8. `Acceptance-test matrix`: inputs, starting state, expected actor/action,
   expected persistent state, attempt-count effect, and expected queue result.
9. `Safety argument`: real trading remains disabled and protected actions remain
   manager-authorized and fail closed.
10. `#170 disposition`: exactly one of supersede, salvage, or close, with the
    deterministic transition.
11. `Protocol footer`: the three machine-readable lines specified below.

Across those sections, the Opus response must:

1. State the root causes and map each observed #168/#169/#170/#171 failure to
   them.
2. Give the one normative recovery architecture in full.
3. Demonstrate how it prevents
   `review FAIL -> Codex REPAIR -> no file changes -> retry -> BLOCKED` for
   non-code remediation.
4. Demonstrate how #120 and then #93/#92/#91 can progress independently when
   safe.
5. Cover all eleven architecture requirements and the Opus-first acceptance
   cases above.
6. Identify any evidence unavailable to Opus and distinguish verified facts from
   inferences; do not fill evidence gaps with assumptions.
7. Specify the zero-diff metadata recovery end-to-end test concretely: a review
   emits a machine-readable PR-metadata blocker; no Codex task is created; the
   manager applies an idempotent metadata mutation; the same fingerprint cannot
   spend a repair attempt; review/CI resumes; and the issue returns to the
   appropriate non-blocked state without a repository diff.

After Opus returns the diagnosis, the accepted text must be persisted to
`docs/ai-team/OPUS_REPAIR_LOOP_DIAGNOSIS.md`, and the accepted architecture must
be appended to `docs/ai-team/DECISIONS.md`, before any recovery implementation
begins. Those persistence edits are a later step and are not part of this
transport-only Codex change.

At the very end of the diagnosis response, and only after the complete diagnosis
and architecture, emit the exact review protocol lines for the transport commit:

```text
REVIEWED_SHA=<exact transport PR head SHA>
VERDICT=PASS
BLOCKERS_JSON=[]
```

Use `VERDICT=FAIL` only if the diagnosis itself identifies a genuine blocker to
supplying the required architecture, and then provide a concise JSON blocker
list. Do not fail merely because this transport document contains no recovery
implementation; implementation is expressly forbidden in Issue #172.
