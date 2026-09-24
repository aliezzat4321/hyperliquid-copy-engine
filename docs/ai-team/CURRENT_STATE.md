# Current State

Generated from `docs/ai-team/state.json`. Do not hand-edit.

**Snapshot:** 2026-09-24T21:37:09Z
**Updated by:** CLAUDE
**Observed main head:** `71525cd7869cb5f91876dd3f856495f73af2df94`
**Mission:** Maximum sustainable executable risk-aware net profitability across the three Hyperliquid lanes.

## Live trading
**DISABLED** — user authorization: **NO**.

## Active priorities
| Priority | Issue | Objective | Builder | Reviewer | Status | Profit-critical |
|---|---:|---|---|---|---|---|
| P0 | #400 | Complete Lane 3 selected-elite Invo trade-source capture | CODEX_CHATGPT | CLAUDE | IN_REVIEW | yes |
| P0 | #403 | Assimilate Moves/Recent discovered portfolios into selector prospectively | CODEX_CHATGPT | CLAUDE | OPEN | yes |
| P0 | #397 | Arbitrate shared Invo API quota without starving Lane 3 | CODEX_CHATGPT | CLAUDE | OPEN | yes |
| P0 | #401 | Enforce Lane 3 source-completeness and event-recall merge gate | CODEX_CHATGPT | CLAUDE | IN_PROGRESS | yes |

## Lane 1 — Hyperliquid native discovery and prospective copying research
**Status:** `SHADOW_EVIDENCE_MEASUREMENT_INCOMPLETE`

| Fact | Value | Observed | Source |
|---|---:|---|---|
| Lane 1 Opus-repair PR open | True | `2026-09-14T23:02:08Z` | PR `#317` |
| Lane 1 repair head | c47bc6854534dd41c620bca69054d4ed9b764940 | `2026-09-14T23:02:08Z` | PR `#317` |
| last measured approved prospective candidates | 0 | `2026-09-11T16:55:17Z` | run `34624217892` |
| real trading enabled | False | `2026-09-14T22:39:38Z` | issue `#130` |

**Blocker:** PR #317 contains the proposed Opus repair but is still open and unaccepted; fresh prospective replay/runtime evidence and an exact-SHA lane-specific Opus PASS remain required before profitability conclusions.
**Next:** Issue #313.

## Lane 2 — Third-party identity resolution
**Status:** `SHADOW_EVIDENCE_MEASUREMENT_INCOMPLETE`

| Fact | Value | Observed | Source |
|---|---:|---|---|
| last measured durable verified identities | 26 | `2026-09-11T16:55:20Z` | run `34624217892` |
| Lane 2 Opus-repair PR open | True | `2026-09-14T23:02:08Z` | PR `#318` |
| Lane 2 repair head | d30772140f20cc5a6aa9d8bc9d8f02f372517863 | `2026-09-14T23:02:08Z` | PR `#318` |
| real trading enabled | False | `2026-09-14T22:39:38Z` | issue `#130` |

**Blocker:** PR #318 contains the proposed identity-safety and capacity repair but remains open; actual post-repair Lane 2 shadow throughput/runtime proof and an exact-SHA lane-specific Opus PASS are still required.
**Next:** Issue #312.

## Lane 3 — Direct Invo notification shadow copying
**Status:** `SHADOW_EVIDENCE_MEASUREMENT_INCOMPLETE`

| Fact | Value | Observed | Source |
|---|---:|---|---|
| integrated RC live authorization / real trading | authorization NONE / REAL_TRADING_ENABLED=NO | `2026-09-22T14:46:17Z` | commit `39d53c57f65a4dfae4eac52f60de5ed15d20e30a` |
| reset/deploy/health pytest tests passing | 18 | `2026-09-22T14:46:17Z` | commit `39d53c57f65a4dfae4eac52f60de5ed15d20e30a` |
| focused strict lifecycle/projector subtests passing | 22 | `2026-09-22T14:46:17Z` | commit `39d53c57f65a4dfae4eac52f60de5ed15d20e30a` |
| AI-team contract pytest tests passing | 61 | `2026-09-22T14:46:17Z` | commit `39d53c57f65a4dfae4eac52f60de5ed15d20e30a` |
| integrated RC full executor subtests passing | 298 | `2026-09-22T14:46:17Z` | commit `39d53c57f65a4dfae4eac52f60de5ed15d20e30a` |
| integrated Lane 3 RC implementation commit | 39d53c57f65a4dfae4eac52f60de5ed15d20e30a | `2026-09-22T14:46:17Z` | commit `39d53c57f65a4dfae4eac52f60de5ed15d20e30a` |
| feed watchdog rebuilt on per-page/per-signal heartbeats plus bounded Hyperliquid HTTP requests | validated at 8c8709d: executor 320/320 PASS; Lane 3 health pytest 2/2 PASS | `2026-09-24T08:23:07Z` | commit `8c8709d03cde3a9cfc0256bf667b29b5b140ca18` |
| feed-originated signals admitted on causal elite qualification alone; direct-watch admission index gates only elite_direct:-sourced signals | validated at 67b1200 on branch fix/lane3-feed-primary-shadow, not yet reviewed or merged: executor 331/331 PASS; git diff --check clean | `2026-09-24T13:33:33Z` | commit `67b1200aa8777b5741423e7e785e09045510b157` |
| feed-primary admission fix (67b1200) independently re-verified: executor npm run check (typecheck+331/331 tests) PASS, git diff --check clean, and Lane 3 Python health validator (tests/test_validate_lane3_shadow_health.py) 2/2 PASS plus a manual LANE3_SHADOW_HEALTH_OK run on Python 3.10.12 | PASS | `2026-09-24T14:35:27Z` | commit `67b1200aa8777b5741423e7e785e09045510b157` |
| feed polling and direct watch paced by one account-wide Invo token bucket with structural feed priority, adaptive 429 cooldown and an AIMD rate floor | builder-validated at 7f6b141 on branch fix/lane3-ingestion-rate-budget, not yet opened as a PR, reviewed or CI-run: direct watch can neither consume the configured feed reserve nor overtake a queued feed request, a feed 429 gates reconciliation in full while a direct-watch 429 costs the feed only its configured share, the direct-watch watchdog bound is taken at the rate floor so an adaptive reduction cannot fire a false stall, and the internal pump now wakes on a higher-priority arrival so a queued DIRECT_WATCH head can no longer pace a later FEED request at reconciliation's refill rate | `2026-09-24T21:37:09Z` | commit `7f6b141bd614adb34d3edd70a89d112921b696bc` |
| executor npm run check subtests passing (typecheck clean) | 369 | `2026-09-24T21:37:09Z` | commit `7f6b141bd614adb34d3edd70a89d112921b696bc` |
| Lane 3 shadow-health validator pytest tests passing | 3 | `2026-09-24T21:37:09Z` | commit `7f6b141bd614adb34d3edd70a89d112921b696bc` |
| Invo API surfaces pinned to a declared budget class by the enforced call-site inventory test, checked against source in both directions | 8 | `2026-09-24T21:37:09Z` | commit `7f6b141bd614adb34d3edd70a89d112921b696bc` |
| feed-primary admission fix 67b1200 merged to main via PR #413 | True | `2026-09-24T19:47:10Z` | commit `71525cd7869cb5f91876dd3f856495f73af2df94` |
| Lane 3 final source-capture implementation commit | bbc574f | `2026-09-21T19:49:53Z` | commit `bbc574feb7ff31bcc1026e1c3522f4d9887a17aa` |
| builder-validated executor subtests passing | 211 | `2026-09-21T19:49:53Z` | commit `bbc574feb7ff31bcc1026e1c3522f4d9887a17aa` |
| verified Invo trade-feed surfaces | 4 | `2026-09-17T23:46:12Z` | issue `#400` |
| exploratory feed-exposed portfolios missing from current selector | 109 | `2026-09-17T23:46:12Z` | issue `#403` |
| exploratory missing portfolios meeting frozen selector on visible evidence | 53 | `2026-09-17T23:46:12Z` | issue `#403` |
| real trading enabled | False | `2026-09-17T23:10:50Z` | manual `VM runtime env 2026-09-17T23:10Z` |

**Blocker:** Main has advanced to 71525cd7869cb5f91876dd3f856495f73af2df94 (merge of PR #413), which carries the integrated Lane 3 RC implementation 39d53c57f65a4dfae4eac52f60de5ed15d20e30a, the no-cap/watchdog repair 8c8709d03cde3a9cfc0256bf667b29b5b140ca18 and the feed-primary admission fix 67b1200aa8777b5741423e7e785e09045510b157. The RC's causal source-time admission, admission-index v2, direct-watch state v8, the managed-position sourceBaseShortId write/read schema repair and bounded atomic-publication generation retention all remain in force: malformed lifecycle variants fail closed, deployment uses the strict atomic publication reader, and the publication reader accepts only the exact safe writer generation-id format, rejecting path-segment traversal before report/ledger reads. All prior strict selector/audit, canonical candidate/admission, runtime-health profitability, atomic publication, source capture, funding, replay, direct-watch, dedupe, readiness, reset and live-off invariants remain intact. The 298/22/18/61 subtest counts pinned to 39d53c57 are superseded by the 369/369 executor count recorded above. Open item: branch fix/lane3-ingestion-rate-budget is exactly three commits ahead of main (66b83b3, 89df023, 7f6b141bd614adb34d3edd70a89d112921b696bc). The prior snapshot recorded this work under a commit SHA, 92092ff9b6b723f7c29240392c4af9483e439286, that was never actually created on this branch — that reference has been corrected to the real head, 7f6b141bd614adb34d3edd70a89d112921b696bc, which replaces direct-watch's private token bucket with a single account-wide coordinated Invo request budget covering feed polling and reconciliation and additionally fixes a scheduler-level priority-inversion bug found in PR #414 review: the pump's sleep was not interruptible, so a DIRECT_WATCH request already queued when the pump went to sleep could still pace a FEED request that arrived and became servable moments later, at reconciliation's slower refill rate rather than the feed's own. `acquire()` now wakes an already-sleeping pump whenever an arriving request is servable before the pump's current deadline; a deterministic regression test queues DIRECT_WATCH first, lets the pump park on its refill deficit, then enqueues FEED and asserts it is granted at its own one-token refill rather than behind direct-watch's reserve refill. Before this branch, the configured 12 req/s envelope with 4 req/s reserved for feed ingress was an assumption rather than an enforced property, so a reconciliation burst could spend the account allowance and 429 the primary feed admission path while direct-watch's own bucket still reported itself healthy. Feed priority is now structural (reserved tokens are unreachable by reconciliation and a feed request can only wait behind another feed request) and survives the scheduler (the interruptible-pump fix above), 429 handling honours a bounded server Retry-After and halves the sustained rate to an AIMD floor with additive recovery, a class in cooldown is rejected locally without spending a token, a saturated budget fails closed on a bounded wait, and an enforced call-site inventory test pins every Invo surface to a declared budget class so the ceiling cannot be silently re-inflated. Resident oversubscription is reported, never capped, and there is still no trader cap. This branch has not been opened as a PR, has had no independent exact-SHA review and has not run GitHub CI; every check recorded here is builder-run on this worktree. Network and gh access were unavailable in this session, so neither Issue #397's current state nor GitHub's main head could be re-observed: head_observed is taken from the local origin/main ref, and the priorities table still reflects the last observed Issue ownership. This is deterministic implementation and load-test evidence only, not profitability evidence and not runtime recall proof. Remaining gates: open a PR, GitHub CI, and BOTH independent exact-SHA reviews must PASS before shadow-only deployment. Real trading is OFF and no live permission, routing, signing, credential, safety threshold or capital setting was touched.
**Next:** Issue #400.

## Infrastructure
**Status:** `DEGRADED`

| Fact | Value | Observed | Source |
|---|---:|---|---|
| targeted #334 renderer + contract + Ruff checks passed | 100.0% | `2026-09-14T23:09:49Z` | run `34907540686` |
| owner-authorized market-shadow reset task exists | True | `2026-09-14T17:28:43Z` | issue `#310` |
| previous 100% storage measurement is no longer a current-state proof | True | `2026-09-14T23:02:08Z` | issue `#310` |
| repository CI baseline repair open | True | `2026-09-14T23:10:55Z` | PR `#335` |
| runtime reports unrelated work continuing | True | `2026-09-14T22:39:38Z` | issue `#130` |
| real trading enabled | False | `2026-09-14T22:39:38Z` | issue `#130` |

**Blocker:** PR #335 has already cleared the obsolete Opus-script Ruff debt and stale generated-state contract failure. Its first full CI run exposed only typed-fact ordering assumptions in two contract tests; this snapshot restores the expected percentage/count ordering while keeping facts current and sourced.
**Next:** Issue #334.

## Update rule
Every fact above carries its own `observed_at` and source reference. The builder of any PR that materially changes these facts updates `state.json` in the same PR, with provenance. The independent reviewer verifies it. `scripts/render_ai_team_state.py` regenerates this file and CI rejects both drift and a snapshot older than the bound in `scripts/ai_team_contract.py`.
