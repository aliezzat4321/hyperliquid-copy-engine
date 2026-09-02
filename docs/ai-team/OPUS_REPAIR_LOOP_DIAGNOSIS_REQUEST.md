# Claude Opus Repair-Loop Diagnosis Request

Status: diagnosis request only. This document contains no proposed implementation or
architecture and authorizes no runtime change.

## Assignment

Perform an independent Claude Opus diagnosis for GitHub Issue #172. The diagnosis must
explain the repeated autonomous-control failures associated with Issues #168 and #170
and PRs #169 and #171, then prescribe one minimal, testable recovery architecture before
any further control-plane implementation is attempted.

Task classification: `UNRESOLVED_DISAGREEMENT`.

The response must be authored by Claude Opus. Treat prior Codex and Sonnet conclusions
as evidence to test, not conclusions to inherit.

The transport PR body must use the non-closing reference `Refs #172`; it must not use
`Closes #172` or another closing keyword. Issue #172 remains open until the complete
Claude Opus diagnosis described below lands.

## Evidence to inspect

Keep inspection limited to the following relevant evidence:

- Sonnet blockers and the subsequent repair chains on PR #169 and PR #171.
- The current repair, review, CI, handoff, retry, blocking, and queue-selection paths in
  `scripts/ai_team_orchestrator.py`.
- Narrow related tests in `tests/test_ai_team_orchestrator.py`.
- The accepted operating description in `docs/ai-team/AUTONOMOUS_TEAM.md` and relevant
  control-plane entries in `docs/ai-team/DECISIONS.md`.
- The task and commit histories for #168, #170, and #166, including repeated Codex repair
  passes and their recorded blocker/result metadata.
- The dependency and queue state that has prevented #120 storage work from proceeding,
  and the downstream effect on #93, #92, and #91.

Do not infer blocker type or required authority from an Issue/PR title or unconstrained
free-form prose when structured GitHub, CI, review-result, ledger, or policy evidence is
available.

## Questions the diagnosis must answer

1. What small, finite set of blocker/remediation classes covers the observed failures?
2. Which deterministic inputs classify each blocker, and which inputs must be
   machine-readable rather than inferred from prose?
3. For every class, which actor owns the next action: Codex code repair, manager PR
   metadata mutation, trusted-manager protected action, CI retry, reviewer re-run, or a
   genuine terminal blocker?
4. Why does the current path turn review or CI failure into Codex `REPAIR` even when no
   repository change is required?
5. Why does `agent produced no file changes` consume attempts until `BLOCKED`, and what
   exact progress, retry, and idempotency rules prevent the same unchanged blocker from
   consuming another attempt?
6. How should PR metadata fixes, protected-path actions, CI reruns/transient failures,
   reviewer reruns, trusted-manager actions, and policy/state reconciliation proceed
   without pretending they are code changes?
7. How must queue isolation work so control-plane maintenance cannot block unrelated
   storage or profitability work unless it demonstrably prevents safe execution?
8. What deterministic migration recovers currently blocked #170, #168, and #166 and
   resumes #120 without chat or manual babysitting?
9. What is the smallest implementation scope after the diagnosis: exactly which files
   and functions would change, and which files/subsystems must not change?
10. Which acceptance tests prove the architecture, including an end-to-end case where a
    PR-metadata blocker is repaired automatically with zero repository file changes?
11. Why does the architecture fail closed while preserving all existing protected-path
    controls and `REAL_TRADING_ENABLED=NO`?
12. Should #170 be superseded, salvaged, or closed after this diagnosis, and why?

## Required diagnosis format

Return one architecture, not a menu of alternatives. The written diagnosis must include:

- A blocker/remediation classification table with deterministic evidence, actor, action,
  completion predicate, retry budget, and terminal condition for every class.
- A root-cause trace that accounts for all observed repeated failures, including the
  sequence `review FAIL -> Codex REPAIR -> no file changes -> retry -> BLOCKED`.
- Exact idempotency keys and state-progress predicates. Repeating an identical action
  against an unchanged blocker must not consume another attempt.
- Queue-isolation and scheduling semantics, including the narrow conditions under which
  one lane may safely block another.
- A migration procedure for #170/#168/#166 and automatic resumption of #120, followed by
  independent continuation of #93/#92/#91 when their own gates allow it.
- The minimal future implementation file/function list and an explicit do-not-change
  list.
- Acceptance tests with initial state, event/action sequence, and final assertions. At
  least one test must cover a PR-metadata remediation that expects zero code changes and
  still reaches the next valid state automatically.
- A fail-closed safety argument and a single recommendation for #170.

Claude Opus must record the complete written diagnosis in the durable in-repository
artifact `docs/ai-team/OPUS_REPAIR_LOOP_DIAGNOSIS.md`; a review transcript or the
exact-SHA review output (`VERDICT`/`BLOCKERS_JSON`) is not the diagnosis artifact and is
not sufficient for completion. When that diagnosis is accepted, the accepted
architecture must also be appended to `docs/ai-team/DECISIONS.md` in the same diagnosis
change. Those diagnosis-stage documentation updates are requirements for the Opus
handoff, not changes authorized for this request-only transport PR.

## Boundaries

This task produces diagnosis only. Do not implement the architecture, modify runtime or
control-plane code, edit workflows, or change trading, storage, credentials, capital
controls, or external project-management integration. Do not weaken tests or policy.

Real trading remains disabled. The diagnosis must not create, infer, extend, or bypass
live-trading authorization. Protected-path controls and independent exact-SHA review
remain mandatory.

The diagnosis is complete only when Claude Opus has answered every required question in
one concrete design, stored it in `docs/ai-team/OPUS_REPAIR_LOOP_DIAGNOSIS.md`, and
appended the accepted architecture to `docs/ai-team/DECISIONS.md`. This request document
itself is only the transport artifact for that review and must not be treated as the
diagnosis.
