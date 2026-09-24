# Decisions Log

Append-only record of accepted architecture / policy decisions. New decisions may supersede old ones but should not erase them.


## 2026-09-17 — Lane 3 selector v3 governs prospective shadow admission

- Broad Invo discovery and leaderboard collection remain research inputs only. Leaderboard
  membership never authorizes a copied position by itself.
- Selector `invo-portfolio-hybrid-v3-20260916` evaluates each portfolio independently and
  requires the versioned hybrid win-rate/return/sample policy: at least 20 closed positions,
  at least 7 age days when known, win rate at least 60%, positive displayed return, no
  liquidation, and quality score at least 60. Lower win rates require both larger returns
  and deeper samples; sibling portfolios cannot inherit eligibility from one another.
- Shadow NEW/ADD is authorized only when the exact source `portfolioId` has fresh,
  non-future `ELITE_CANDIDATE` evidence that existed before the source trade. Both the
  aggregate candidate state and that portfolio's own observation are age-bounded and fail
  closed when stale, missing, malformed or future-dated.
- Existing managed exposure may CLOSE after demotion. Eligibility therefore constrains new
  risk without orphaning exposure. Selector-version changes reset first-elite timestamps so
  historical outcomes cannot be relabelled as prospectively selected.
- Close reconciliation distinguishes true non-executable dust from executable residuals:
  insufficient causal displayed depth remains `UNRESOLVED_EXPOSURE` and retryable rather
  than being erased as dust; missing or mismatched Hyperliquid L2 coin identity fails closed.
- This decision governs prospective shadow evidence only. It does not authorize real
  trading, capital, credentials, signing or live order routing; `REAL_TRADING_ENABLED`
  remains disabled.

## 2026-09-17 — Lane 3 feed surfaces have independent prospective boundaries

- The supported `/v1_0/posts/get_feed` discovery filters are the exact current web-app
  enum values `following`, `trending`, `fire_moves`, and `most_recent`. Generic feeds are
  discovery and latency supplements; portfolio-specific investment polling remains the
  authoritative monitor for already selected elite portfolios.
- Every feed surface owns a separate durable high-water cursor. A surface with no cursor
  establishes its own prospective baseline: historical OPEN/ADD and unowned CLOSE events
  are indexed but not executed, while CLOSE for locally managed exposure may reconcile.
  Later posts newer than that surface's boundary enter the normal freshness, causal elite
  admission, source-event dedupe, and execution-realism gates.
- Adding or re-enabling one surface must not depend on process-global initialization and
  must not advance another surface's cursor. Unrecoverable gaps retain the existing
  owned-close-only reconciliation and fail-closed cursor semantics.
- Feed rotation and direct hydration stay bounded and retain exponential HTTP 429 backoff.
  This decision changes no real-trading permission, credential, capital, signing or order
  route; `REAL_TRADING_ENABLED` remains disabled.

## 2026-09-18 — Lane 3 direct watch is boundary-proven and failure-isolated

- The captured portfolio-investments endpoint is treated as newest-first across pages.
  The sanitized two-portfolio, three-page read-only observation supporting that assumption
  is retained in `services/invo-notification-executor/test/fixtures/invo-closed-ordering-observation.json`;
  it is runtime evidence, not a permanent API contract. Every fetched CLOSED page is
  therefore checked for within-page and cross-page non-increasing effective close time.
  An ordering violation emits explicit risk telemetry and advances neither baseline nor
  watermark.
  A closed-history timestamp boundary is proven only by a strictly older row or endpoint
  exhaustion; stored identities cannot prove an unstable equal-timestamp ordering.
  Unrelated equal-timestamp rows cannot advance the watermark. Bounded pagination that
  proves none of these retains the prior watermark and emits overflow-risk telemetry.
- Selector timestamps are prioritization hints only. Non-rate-limit selector, open, and
  closed request failures are isolated per target. Scheduling attempts are durably noted
  before I/O to rotate persistent failures without advancing event watermarks. HTTP 429
  retains bounded global cooldown and records work skipped by that cooldown.
- Cross-source event identity includes lifecycle action; INCREASE also includes resulting
  source size. Legacy actionless keys suppress replay of old OPEN events but cannot
  suppress an ambiguous same-time INCREASE or CLOSE; prospective feed cursors and direct
  watermarks provide the history-replay boundary. The separate source-close lifecycle key
  remains and is canonical completion evidence for a CLOSED lifecycle even when feed and
  direct timestamps differ. A per-source queue serializes feed and direct lifecycle
  execution while distinct sources retain parallelism.
- A surface's first fetched snapshot durably establishes its prospective feed baseline
  immediately after indexing. Retryable owned CLOSE recovery may still gate its cursor,
  but cannot leave the surface in startup mode and swallow later fresh OPEN/ADD events;
  the managed exposure retains its pending close for reconciliation.
- The 45-target defaults imply nominal 18s open and 45s closed sweeps. Complete OPEN
  pagination now permits at most three pages per each of eight bounded open/drain slots;
  with four selector requests and two pages for each of three CLOSED slots, the explicit
  worst case is 34 requests per 3s scan (11.33 requests/s). This is a ceiling, not an
  expected rate: short endpoints stop early and a 429 stops the scan under bounded global
  cooldown. Health exposes configured page bounds, the calculated ceiling, oldest poll
  age, retirement drain counts, overflow, and ordering failures. Shared-quota arbitration
  remains unresolved under open Issue #397; this PR does not claim that capacity proof.
- This changes shadow source capture only. It does not authorize real trading, capital,
  credentials, signing, order routing, deployment, or bulk mining.

## 2026-09-20 — Lane 3 direct-watch residency is capped and absence is non-authoritative

- The default `trending,all` bounded discovery pages are a sampler, not an authoritative
  universe. A resident target absent from a fresh cycle remains in its current lifecycle
  and continues identical OPEN/CLOSED polling. Only a fresh, structurally valid current-cycle
  non-ELITE row for that exact portfolio is negative evidence. Two distinct observation
  timestamps spanning at least the 10-minute collector interval are required before
  `RETIRING`; one observation enters `MISSING_GRACE`, and fresh ELITE evidence immediately
  restores `ACTIVE` without resetting causal watermarks or dedupe/selector state.
- `MAX_DIRECT_WATCH_RESIDENT_TARGETS` defaults to 48 and covers `ACTIVE`, `MISSING_GRACE`,
  and `RETIRING`. At the configured 3s scan, eight OPEN hydrations and 18s OPEN cadence,
  the sustainable OPEN ceiling is 48; three CLOSED hydrations and 60s cadence yield 60.
  Startup rejects a cap above either ceiling. A full cap never evicts an incumbent: new
  ELITE candidates are deterministically deferred with durable state and audit telemetry.
- A safely drained retirement becomes a compact nonresident tombstone instead of being
  deleted. The tombstone retains prospective baseline, OPEN/CLOSED high-water and boundary
  identity, selector metadata, source identity, and demotion provenance. Re-enrollment
  restores those maxima while rebasing both event watermarks prospectively (and clearing
  a superseded CLOSED equal-time boundary), preventing historical replay. Existing v1-v4
  state migrates in place; startup fails closed if its
  resident count already exceeds the configured cap.
- This remains shadow-only. It changes no real-trading permission, bulk-miner setting, or
  candidate-universe policy; Issues #397 and #403 remain separate.

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

## 2026-09-18 — Lane 3 admission history is a bounded causal index

- Portfolio research remains the owner of the append-only candidate snapshot JSONL, but
  atomically publishes a separate `*.recent.json` admission index containing at most the
  last three observations per portfolio from the last 30 minutes.
- Three observations cover a fresh signal (maximum age 25 seconds) across the collector's
  10-minute refresh boundary. Admission selects only the latest observation at or before
  source time, preserving demotion and preventing look-ahead.
- The executor never parses the historical JSONL. The compact index has an 8 MiB read
  ceiling; missing, malformed, oversized, or unreadable indexes fail closed. Thus hot-path
  memory and latency do not grow with historical runtime.
- This changes no live-trading permission. `REAL_TRADING_ENABLED` and
  `NOTIFICATION_TRADER_LIVE` remain disabled.

## 2026-09-20 — Lane 3 direct watch is deadline-scheduled and locally request-budgeted

- OPEN and CLOSED target work shares one earliest-deadline queue and a fixed worker
  pool. Pagination remains sequential within a target, while targets execute
  concurrently with per-target failure isolation.
- Every direct `/get_investments` HTTP attempt consumes an executor-local token before
  dispatch. The default 12 requests/second envelope reserves 4 requests/second for
  feed/selector ingress, leaving an 8 requests/second direct allowance with burst 32.
  A 429 starts global cooldown, removes burst credit, pauses new work, and makes new
  admissions fail closed until observed deadline health recovers.
- Resident admission uses the hard minimum across configured residents, OPEN and CLOSED
  max pages, the two-attempt request-timeout bound, concurrency, request rate/burst,
  unchanged deadlines, per-scan work bounds, and fixed feed/selector reserve. Defaults
  prove 16 residents; `MAX_DIRECT_WATCH_RESIDENT_TARGETS=48` is only an outer ceiling.
- The proof is configuration evidence, not runtime/source-recall evidence. #397 remains
  required before bulk Invo miners can be re-enabled, and #401 still requires
  post-deploy prospective recall proof.
- This changes no live-trading permission. `REAL_TRADING_ENABLED` and
  `NOTIFICATION_TRADER_LIVE` remain disabled.

## 2026-09-20 — Superseding Lane 3 capacity and admission-health decision

- This decision explicitly supersedes the earlier same-day statements that 48 residents
  were sustainable, that startup rejected only above 48, and that fresh evidence
  immediately restored `ACTIVE`. The outer configured value 48 is not an executable
  capacity claim. With the final defaults the hard-proven resident cap is **16**; fresh
  evidence re-enters `ENROLLING`, and only complete prospective CLOSED then OPEN
  baselines can restore `ACTIVE` lifecycle state.
- The proof assumes OPEN at most 3 pages, CLOSED at most 2 pages, at most 2 attempts per
  page, a 2-second request timeout, 16 target workers, an 18-second OPEN deadline, a
  60-second CLOSED deadline, 2 seconds of fixed overhead, a 12 request/second envelope,
  burst 32, and a fixed 4 request/second feed/selector reserve. OPEN and CLOSED share one
  earliest-deadline queue. At cap, the conservative wall-clock bounds are 14 seconds for
  OPEN and 22 seconds for the combined OPEN+CLOSED sweep; steady-state direct demand is
  below the remaining 8 request/second budget. The unchanged signal-age and endpoint
  completeness gates still apply.
- Capacity and authorization health are pinned independently of candidate-state
  authority. Construction uses the hard cap and publishes an empty admission index.
  Every scan atomically suspends NEW/ADD authorization before work. Missing, stale, or
  malformed candidate state cannot relax the cap or enable admission. Authorization is
  republished only after an authoritative candidate read and complete successful OPEN
  and CLOSED observations inside both deadlines. Attempt timestamps exist only for fair
  scheduling and never count as freshness.
- A 429/cooldown, deadline violation, capacity violation, token preflight failure,
  unexpected 401, target error, incomplete pagination, or scan-level source failure
  leaves the admission index empty without deleting targets or lifecycle watermarks.
  Token freshness is preflighted for the full 22-second scan horizon plus the client's
  30-second refresh margin and one request timeout. The preflight refresh is covered by
  fixed overhead; direct-watch 401 retry is disabled, so an unexpected 401 makes the
  scan unhealthy instead of silently consuming an unbudgeted retry.
- Direct-watch journal v2 assigns monotonic sequences and snapshots record the highest
  applied sequence. Replay ignores entries at or below that sequence, closing the crash
  window between snapshot rename and journal truncation while retaining legacy journal
  migration. CLOSED evidence whose source OPEN predates admission is classified
  `pre_enrollment_close_ignored`, not a selected-elite missed short round trip.
- This is configuration/test evidence, not prospective runtime recall proof. Issues #397
  and #401 remain open gates. No deployment or real-trading permission changed;
  `REAL_TRADING_ENABLED=NO`.

## 2026-09-23 — Lane 3 residency is not an arbitrary trader-count selection gate

- This decision supersedes the configured 48-resident and hard-proven 16-resident
  admission exclusions above. Every causally qualified ELITE portfolio may enter the
  direct-watch resident lifecycle; neither selector admission nor historical admission
  intervals are rejected because a fixed trader or interval count was reached.
- The calculated positive integer `transportTargetCeiling` remains health telemetry for
  the configured timeout, pagination, concurrency, and request budget. It is not an
  authorization cap. Actual overdue OPEN/CLOSED observations and cooldown state still
  suspend new admission publication fail closed, while the deadline-first schedulers
  rotate bounded attempts fairly across all residents.
- Captured signals advance a portfolio watermark only when every signal has a durable
  terminal disposition. A nonterminal signal remains retryable on a later scan without
  preventing an independent portfolio from committing its terminal batch.
- A process-local watchdog detects bounded feed or direct-watch loop silence and exits
  nonzero; systemd restarts only that failure path. Normal operator stops remain stopped.
- This remains shadow-only. `REAL_TRADING_ENABLED` and `NOTIFICATION_TRADER_LIVE` remain
  disabled, and no real-order permission, routing, signing, credential, or capital setting
  changes.

## 2026-09-21 — Lane 3 scan suspension is replay-safe capture, not a terminal decision

- Every direct-watch scan still publishes an unhealthy/empty authorization index before
  source I/O, but the index now records explicit health and suspension reason. Admission
  decisions distinguish `ALLOWED`, structural `TERMINAL`, and health-related
  `TRANSIENT` outcomes. Feed NEW/ADD denied during scan, cooldown, capacity-health, or
  pre-publication suspension remains unseen and cannot advance its surface cursor.
- Direct OPEN/ADD hydration is capture-first. Complete endpoint observations are buffered
  without advancing `processedThroughMs`; CLOSED signals retain their immediate unwind
  path. Only after the whole scan proves healthy are ACTIVE admissions atomically
  published, buffered signals decided against that index, and each target watermark
  advanced after all of its signals reach a durable terminal disposition. A scan error,
  429, incomplete page, or transient execution/admission outcome leaves those signals
  replayable on the next healthy scan.
- ACTIVE-to-RETIRING and MISSING_GRACE transitions retain the original `admittedAtMs` so
  later unowned closes can distinguish pre-enrollment history from a selected-elite short
  round trip.
- The shared worker schedule is now explicitly OPEN-first, then CLOSED, with deadline
  ordering inside each phase. This policy is part of the hard-cap proof: at 16 residents,
  worst-case three-page/two-attempt OPEN work completes in 14 seconds including fixed
  overhead, inside the 18-second OPEN deadline; the following CLOSED work remains inside
  its 60-second deadline. The configured 48 remains only an outer ceiling.
- This is implementation and deterministic-test evidence at commit
  `367d3d8c31cad5d2a40db55d789bec8b37c840c8`, not prospective runtime recall proof.
  Issues #397 and #401 remain open gates. Production and live-trading permissions are
  unchanged; `REAL_TRADING_ENABLED=NO`.

## 2026-09-24 — Lane 3 ingestion is paced by one coordinated Invo request budget with feed priority

- Feed polling and the elite direct watcher consume the **same** Invo account quota, so
  they now share **one** token bucket (`services/invo-notification-executor/src/invo-request-budget.ts`).
  Before this change the direct watcher owned a private bucket and the feed path was
  unpaced, so the configured "12 req/s with 4 req/s reserved for feed ingress" envelope was
  an assumption rather than an enforced property, and a reconciliation burst could consume
  the account's allowance and 429 the feed.
- The feed is the primary class and its priority is structural, not advisory. A
  `DIRECT_WATCH` request is granted only while the bucket holds more than
  `INVO_FEED_RESERVED_REQUESTS_PER_SECOND` tokens, and every scheduling pass serves the
  whole feed queue first. A feed request can therefore only ever wait behind another feed
  request. Since the feed-primary admission fix (PR #413) made the feed the primary shadow
  admission path, reconciliation delaying it is a causal-recall problem, not a tuning
  preference: a missed feed NEW/ADD cannot be made causal by a later reconciliation.
- Within a class the queue is FIFO. A poll-and-retry waiter design lets whichever waiter
  computes the shortest sleep overtake its peers, which starved individual direct-watch
  targets for the full bounded wait at 41 residents even though aggregate throughput was
  fine. Ordered queues make each waiter's delay a function only of the work ahead of it.
- 429 handling is adaptive. A server `Retry-After` wins when present (bounded by
  `INVO_RATE_LIMIT_MAX_RETRY_AFTER_COOLDOWN_MS` so a poisoned header cannot freeze the
  loop); otherwise the cooldown escalates exponentially over consecutive rejections and
  resets after a quiet window. Each 429 also halves the sustained rate down to
  `INVO_MIN_REQUESTS_PER_SECOND`, recovered additively over quiet intervals. The rejected
  class serves the full cooldown; a feed 429 gates reconciliation in full (the account is
  provably limited) while a direct-watch 429 costs the feed only
  `INVO_FEED_COOLDOWN_SHARE` of the penalty.
- Static worst-case bounds that must survive degradation are computed at the AIMD **floor**,
  not the configured ceiling. In particular the direct-watch loop-watchdog limit uses
  `INVO_MIN_REQUESTS_PER_SECOND`, because a bound taken at the ceiling would fire a false
  stall and `process.exit(1)` precisely while the budget was adapting to a real 429.
- A request whose class is in cooldown is rejected locally without spending a token, and a
  saturated budget fails closed on `INVO_REQUEST_MAX_WAIT_MS` rather than stalling a
  watched loop. Redundant traffic is otherwise removed only where provably safe: a rotating
  discovery re-poll of a surface another trigger already fetched within
  `NOTIFICATION_TRADER_FEED_MIN_SURFACE_REPOLL_MS` is skipped, while push hydration,
  startup baselines, gap recovery and owned-close reconciliation are never skipped and a
  suppressed poll neither marks a post seen nor advances the durable cursor.
- Direct-watch OPEN polls are deliberately **not** suppressed on the grounds that the feed
  recently observed the same portfolio. That would create a window in which a feed miss has
  no reconciliation, and it would break the observed-freshness guarantee that gates
  admissions. Rejected on purpose; record it as considered rather than overlooked.
- Resident oversubscription is reported, never capped. There is no trader cap, so when the
  qualified-elite population exceeds the proven transport ceiling the health payload states
  it (`residentCountOversubscribed`, `degradedResidentCap`) and direct-watch admissions fail
  closed on observed deadline health. Feed admission is unaffected, which is the property
  the 41-resident load tests assert.
- `scripts/validate_lane3_shadow_health.py` now fails closed on a missing or uncoordinated
  budget, an unconfigured feed reserve, an exhausted feed wait, and any state in which the
  feed is gated longer than direct watch.
- The budget only bounds the account footprint if every call site uses it, and that is a
  property of the call sites rather than of the budget module. An enforced inventory
  (`test/invo-call-site-inventory.test.ts`) therefore pins every Invo surface to a declared
  class — `BUDGETED_FEED`, `BUDGETED_DIRECT_WATCH`, `UNBUDGETED_AUTH_PRECONDITION`,
  `LIVE_ONLY` or `OFFLINE_CLI` — and checks it against the source in both directions, with
  the declared per-surface call-site count, the live gate guarding each `LIVE_ONLY` call,
  and the rule that only `post()`/`refreshAccessToken()` may reach `fetch`. A new endpoint,
  a second feed read or a raw request now fails a test instead of silently re-inflating the
  ceiling.
- The class charged for an Invo access-token refresh is threaded from the triggering
  request instead of latched in module state. Both ingestion loops run concurrently, so a
  latched "current class" was attributed by whichever loop wrote it last. It never
  mis-gated anything — refreshes are deliberately ungated and the token is spent
  account-wide either way — but a per-class footprint metric that can be wrong is the one
  thing this subsystem exists to report honestly.
- Deterministic implementation and load-test evidence only; not profitability evidence and
  not runtime recall proof. `REAL_TRADING_ENABLED=NO` and `NOTIFICATION_TRADER_LIVE=false`
  are unchanged, and no real-order permission, routing, signing, credential or capital
  setting is touched. Hyperliquid remains the only venue.
