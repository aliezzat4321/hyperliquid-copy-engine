# Incident: acceptance evidence runner deadlock (2026-09-08)

Status: OPEN
Severity: P0 control-plane liveness
Real trading: disabled

## Symptom

Issue #197 entered `POST_MERGE_EVIDENCE` and the orchestrator has emitted `ACCEPTANCE_PHASE_READY` once per minute for hours with `last_error=awaiting deterministic phase runner evidence`. The task never advances, blocks unrelated queued work, and consumes scheduler cycles.

## Root cause visible in current main

`Orchestrator.cycle()` treats every task whose `task_type` is in `EVIDENCE_TASK_TYPES` as a passive consumer of `task.evidence_json["result"]`. When `result` is absent it simply schedules another retry after `poll_seconds`.

There is no bounded retry/liveness budget in that branch, no escalation to a repair task, and no fail-forward path that releases the global scheduler. Therefore a missing or broken deterministic phase runner creates permanent head-of-line blocking.

## Required repair

1. Evidence phases must have a deterministic runner dispatch/heartbeat contract. A phase may wait only when there is proof that its runner exists and is making progress.
2. Missing runner result must increment a durable liveness-attempt counter. It must not reset on every scheduler cycle.
3. After a small bounded liveness budget (default 3 polls, configurable), the evidence task must transition out of `RETRY` into a task-local terminal/recoverable state and enqueue an autonomous `REPAIR` for the control plane. It must not request owner action merely because the runner is broken.
4. The scheduler must continue unrelated ready work while an evidence phase is waiting or being repaired. No single acceptance phase may head-of-line block the entire queue.
5. Emit explicit runtime fields/events: runner identity, last heartbeat, liveness attempts, next retry, escalation issue/task, and whether unrelated work is continuing.
6. If the phase is genuinely waiting for prospective time/data rather than a broken runner, classify that distinctly (`WAITING_EVIDENCE_WINDOW`) with a real next-eligible timestamp; do not poll every minute.
7. Keep acceptance fail-closed: never fabricate or downgrade required evidence just to close an issue.
8. Preserve real trading OFF and all protected-change review/CI gates.

## Regressions

- Missing `evidence_json.result` for an evidence task cannot remain `RETRY` forever.
- Three missing-runner polls cause autonomous repair escalation and release the scheduler for another queued issue.
- A healthy runner heartbeat may keep a phase waiting without consuming the liveness budget incorrectly.
- A future prospective-window timestamp results in `WAITING_EVIDENCE_WINDOW`, not 60-second retry churn.
- An unrelated queued task is dispatched while one evidence phase waits.
- A completed trusted evidence envelope still proceeds through `complete_acceptance_phase()` unchanged.
- No evidence-dependent parent is closed or marked proven without verified canonical evidence.

## Immediate recovery target

#197 must either consume valid trusted post-merge evidence and advance, or be placed into a recoverable evidence-wait/repair state while the scheduler proceeds to other queued P0/P1 work. Infinite minute-by-minute retry is not acceptable.
