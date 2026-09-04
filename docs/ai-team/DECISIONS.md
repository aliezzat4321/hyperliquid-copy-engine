# Decisions Log

Append-only record of accepted architecture / policy decisions. New decisions may supersede old ones but should not erase them.

## 2026-09-03 — Risk eligibility is separate from credible edge

- Promotion policy v2 retains the v1 profitability floors and adds a versioned,
  deterministic risk-governor contract.
- Edge credibility is a required input but cannot select a capital state. Audited and
  complete risk evidence independently limits a candidate to `NO_CAPITAL`,
  `MICRO_CANDIDATE`, `SMALL_CANDIDATE`, or `SCALE_CANDIDATE`.
- Unknown, malformed, stale or deteriorating required evidence fails closed and can
  automatically demote or halt a candidate.
- These states are eligibility ceilings only. They never enable trading or replace the
  owner authorization required by `LIVE_TRADING_GATE.md`.

## 2026-08-31 — AI team operating model
- GitHub is the durable communication and memory layer between ChatGPT/Codex and Claude.
- One builder owns each Issue; the other AI agent is the preferred independent reviewer for profitability-critical work.
- `docs/ai-team/state.json` is the compact current-state source and `CURRENT_STATE.md` is generated from it.
- Full-repository audits are exceptional; routine tasks read the state snapshot, Issue, linked docs and relevant code only.
- Profitability claims follow `PROFITABILITY_STANDARD.md`.
- Real capital requires explicit user authorization under `LIVE_TRADING_GATE.md`.

## 2026-08-31 — Contract hardening after independent review

Independent review of the operating system found that the contract validator enforced
internal consistency between `state.json` and its own renderer, and essentially nothing
about accuracy. Eight adversarial mutations all passed CI, including a three-year-stale
snapshot, a builder reviewing their own work, deleted lane facts, and
`live_trading.authorized` flipped to `true` with `"trust me"` as the approval reference.

Accepted, superseding parts of the 2026-08-31 operating-model entry above:

- `state.json` moves to schema version 2. Lane, infrastructure and storage facts are
  structured records carrying `value`, `unit`, `observed_at`, `source_type` and
  `source_ref` instead of bare prose.
- Validation fails closed: unknown fields, unknown enum members, malformed or future
  timestamps, empty fact lists, placeholder owners and snapshots older than 72 hours are
  all rejected.
- Builder and reviewer are enum'd logical agents and must differ on active work;
  profitability-critical work requires an AI reviewer.
- Live-trading authorization becomes a structured, user-issued, expiring object with a
  formatted `approval_reference`. Agents must never create, infer or extend it.
- A separate `live-sensitive-guard` workflow classifies changes to real-trading
  permissions, order routing, key handling, live systemd environment and safety
  thresholds. It classifies only; it never authorizes.
- Promotion thresholds move into a versioned `quant-promotion-policy-v1`, recorded as
  PROVISIONAL with per-threshold rationale and known weaknesses, so both agents gate on
  the same numbers and a change is a reviewed decision rather than a code edit.
- `SYSTEM_MAP.md` and a machine-readable experiment registry are added so agents can
  locate code and check prior results without re-auditing the repository.
- Review independence is *recorded*, not proved: both agents share one GitHub identity.
  `REVIEW_PROVENANCE.md` documents the limitation and what CI can and cannot check.

## 2026-09-02 — Claude availability is asynchronous; protected merges are not

- Claude rate, usage-cap and provider unavailability is persisted as
  `WAITING_RATE_LIMIT`; bounded repo-free readiness probes and the VM scheduler resume
  the same review checkpoint without occupying a GitHub runner or blocking other work.
- Provider unavailability does not relax the merge gate. AI-control-plane changes still
  require trusted Issue authorization, the exact protected-file allowlist, green CI and
  an independent Claude PASS for the exact target SHA before merge.
- Recoverable automation outcomes must stay inside an autonomous loop: review failure
  queues Codex repair, CI failure queues Codex repair on the same PR, PR movement queues
  an exact-SHA replacement review, merge/API rejection retries the merge stage, provider
  limits wait without consuming failure budget, interrupted workers are reaped and
  requeued, and recoverable manager-side finalize/push/API failures retry within the
  bounded circuit breaker instead of immediately becoming owner blockers. Parent-to-child
  BUILD→REVIEW, REVIEW→REPAIR and stale-SHA→replacement-review handoffs are idempotent and
  reconciled on later orchestrator cycles, so a restart or GitHub mirror/API failure cannot
  strand otherwise recoverable work. Terminal `BLOCKED` is reserved for safety,
  authorization, corrupted task identity/state, or an exhausted bounded failure circuit
  breaker.
- No pre-review `ASYNC_MERGE` path is permitted. Consequently a later asynchronous FAIL
  cannot leave an unreviewed control-plane change active on `main` while awaiting a
  forward repair.
- This decision does not change live-trading permissions. `REAL_TRADING_ENABLED` remains
  disabled, and trading, live, deployment, capital and credential paths remain excluded.

## 2026-09-03 — Typed remediation and explicit Opus-first entry supersede blind repair routing

Issue #172 accepts the single architecture in
`docs/ai-team/OPUS_REPAIR_LOOP_DIAGNOSIS.md` for the next control-plane implementation.

- Review and CI failures must be structured as one of seven fail-closed remediation
  classes: `CODE_CHANGE`, `PR_METADATA`, `PROTECTED_ACTION`, `CI_RETRY`,
  `REVIEW_RERUN`, `POLICY_RECONCILIATION`, or `TERMINAL`. Unknown or contradictory
  input is terminal; title and free-form prose are not classifier inputs.
- Each blocker has a canonical fingerprint and each requested action an idempotency key.
  Re-observing an unchanged fingerprint cannot create another child or consume another
  attempt. Progress is the class-specific postcondition, not the existence of a file
  diff; only `CODE_CHANGE` requires a repository diff.
- Actor follows remediation type: Codex repairs code, the manager changes PR metadata or
  reconciles state, the trusted manager performs separately authorized protected actions,
  CI reruns checks, and the required reviewer reruns exact-SHA review.
- Scheduling and blocking are dependency-component scoped. Control-plane maintenance or
  Opus/provider waiting cannot block unrelated safe storage/profitability work merely by
  occupying a global queue state.
- Initial routing is explicit machine metadata. The approved high-value classes
  `QUANT_PROFITABILITY`, `STATISTICAL_METHODOLOGY`, `MAJOR_ARCHITECTURE`,
  `UNRESOLVED_DISAGREEMENT`, and `CAPITAL_SENSITIVE_METHODOLOGY` may start as Claude
  Opus RESEARCH; routine engineering remains Codex BUILD. Invalid or unauthorized route
  combinations fail closed.
- #170 is superseded rather than salvaged. Legacy #166/#168 state is reconciled from
  authoritative evidence, #120 is automatically released when its own dependencies are
  satisfied, and #93/#92/#91 proceed through their class-appropriate routes.
- This is an architecture decision, not implementation authorization. Existing exact-SHA
  review, CI, protected-path and live-trading gates remain intact;
  `REAL_TRADING_ENABLED` remains disabled.

Implementation note for Issue #178: the accepted `BLOCKER_V1` router is now the
control-plane contract. Legacy trusted queue entries are migrated through the reviewed
class allowlist, deterministic CI failures remain autonomous `CODE_CHANGE` work, and
protected workflow targets come only from repository configuration after a trusted,
unexpired, exact-SHA Issue authorization is verified. Model-emitted blocker data cannot
select a workflow or ref. This changes no live-trading permission.


## 2026-09-03 — Exact-SHA reviewer PASS + green CI is the merge decision for recognized task classes

This supersedes the earlier task-class policy that withheld automatic merge from non-routine / Opus-class work after successful review.

- Builder and reviewer responsibilities remain separate: Codex implements; the routed independent Claude reviewer evaluates the immutable PR head SHA.
- For every recognized task class (`ROUTINE`, `QUANT_PROFITABILITY`, `STATISTICAL_METHODOLOGY`, `MAJOR_ARCHITECTURE`, `UNRESOLVED_DISAGREEMENT`, and `CAPITAL_SENSITIVE_METHODOLOGY`), an independent exact-SHA `PASS` plus green required CI is the merge decision. The credential-holding manager executes that decision immediately; it is not a second approval stage and no timer, human click, or separate finalizer is required.
- `UNCLASSIFIED` or invalid task classes remain ineligible for automatic merge.
- Protected AI-control-plane changes still require trusted Issue authorization, `AI_TEAM_PROTECTED_CHANGE=YES`, and the narrow `AUTO_APPLY_CONTROL_PLANE_PATHS` allowlist before merge. Workflow, systemd deployment, trading/live, capital, credential, and other paths outside that allowlist remain fail-closed.
- PR-head movement invalidates the old review; merge/API rejection retries only the merge stage against the exact reviewed SHA.
- This changes no live-trading permission. `REAL_TRADING_ENABLED` remains disabled.

## 2026-09-03 — Canonical storage accounting and lossless tape lifecycle

- Storage `used_pct` is `used / (used + f_bavail)`, matching `df -P`; available bytes are
  `f_bavail`, never privileged `f_bfree`.
- Historical market-tape compaction is lossless and exact-SHA reviewed. Lossy downsampling
  is deferred because it can change the liquidity evidence visible to copyability replay.
- Durable fills capture is `NEVER_STOP`; pressure responses are emitted per writer.
- Dataset budgets plus unallocated reserve must fit below each mount's target-used band.
  Unaccounted filesystem bytes are explicitly measured so unnamed growth fails closed.

## 2026-09-03 — Storage closure uses one machine-readable exit gate

- The `market-shadow` byte budget is 11 GiB with a 9 GiB steady-state bound so declared
  budgets plus reserve fit the measured 75% target band without weakening thresholds.
  Lossless tape lifecycle is therefore mandatory before restart; inability to reach the
  bound requires reviewed retention or volume growth.
- Historical PostgreSQL compaction fails closed on unrecoverable blank payloads, schema
  drift, retained WAL, replication slots and insufficient WAL-aware headroom.
- Issue #120 can close only when the read-only exit-gate report passes all apply,
  provenance, policy, safety and uncontaminated 24-observation stability conjuncts.
- This decision changes no live-trading permission. `REAL_TRADING_ENABLED` remains
  disabled.

## 2026-09-03 — Dual-Codex review pipeline and selective Claude gates

- Routine work is Codex build, deterministic preflight, independent exact-SHA Codex
  review, then CI. It has no Claude dependency.
- The reviewer uses a fresh process and separate clean, read-only worktree, receiving the
  bounded review packet but never the builder transcript.
- ENGINE_CRITICAL adds Sonnet after Codex. QUANT freezes an Opus pre-build contract and
  retains Sonnet plus a separate final Opus decision. DESTRUCTIVE retains final Opus.
- Runtime status exposes codex_builder, codex_reviewer, sonnet and opus while retaining
  legacy codex/claude fields for #130. A Claude rate limit parks only its task.
- Deterministic failures route directly to Codex with SHA/check/detail fingerprints;
  CODE_CHANGE permits at least three attempts before genuine no-progress termination.
- Protected-path, storage, exact-SHA, CI and live-sensitive gates remain fail closed;
  real trading remains disabled.

Issue #205 implementation clarification: scheduler serialization is per execution slot,
not global. SQLite atomically leases one task in each of `codex_builder`,
`codex_reviewer`, `claude_specialist`, and `manager`; detached workers run after the
short scheduler flock is released. The deterministic preflight is repository Ruff,
format, full pytest, AI-team contract validation, and live-sensitive classification.
Legacy `MAJOR_ARCHITECTURE` maps to `DESTRUCTIVE` so storage #120 retains exact-final
Opus, and protected v2 bootstrap changes retain a fresh Sonnet challenge.
