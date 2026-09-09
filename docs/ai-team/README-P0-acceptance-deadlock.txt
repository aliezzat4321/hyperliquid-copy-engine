P0 ACCEPTANCE-RUNNER DEADLOCK / THREE-LANE DATA-COLLECTION INVARIANT

The acceptance/evidence control plane must never be a prerequisite for Lane 1, Lane 2, or Lane 3 shadow data collection.

Current observed defect (2026-09-09): bounded liveness quarantine marks a missing-result acceptance assignment STALE after 3 polls, but rollout reconciliation can recreate an identical POST_MERGE_EVIDENCE/MEASUREMENT assignment. This converts the former infinite single-assignment retry into repeated assignment churn and still delays useful work.

Permanent requirements:
1. Shadow collectors are independent, persistent jobs/services. They continue collecting immutable execution-realistic observations even if acceptance/evidence evaluation is unavailable.
2. Acceptance/evidence runners consume collector output asynchronously; they never gate collector execution.
3. Deterministic phase dispatch must either invoke a real runner and persist a trusted result envelope, enter an explicit future evidence window with exact next_eligible_at, or fail/quarantine task-locally.
4. Reconciliation must deduplicate by stable assignment fingerprint (issue, task_type, target SHA/contract/requirement). A STALE/quarantined fingerprint MUST NOT be recreated until there is a material new cause (new target SHA/contract, explicit repair completion, or exact next_eligible_at for a genuine future window).
5. No waiting/retrying/quarantined acceptance task may prevent unrelated ready work or any of the three shadow collectors from running.
6. Lane 1 (#93), Lane 2 (#92), and Lane 3 (#91) direct shadow collection is the immediate priority. Real trading remains disabled.
7. Required regression: quarantine an assignment, run reconciliation repeatedly, prove the identical fingerprint is not re-enqueued; meanwhile prove all three collectors continue producing fresh records.

Do not treat bounded retry alone as resolution. The incident is resolved only when fresh Lane 1/2/3 shadow data accrues continuously while acceptance evaluation can independently fail/recover.
