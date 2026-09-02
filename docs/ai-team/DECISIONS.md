# Decisions Log

Append-only record of accepted architecture / policy decisions. New decisions may supersede old ones but should not erase them.

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
