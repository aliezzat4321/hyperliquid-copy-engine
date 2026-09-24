# Current State

Generated from `docs/ai-team/state.json`. Do not hand-edit.

**Snapshot:** 2026-09-24T08:23:07Z
**Updated by:** CLAUDE
**Observed main head:** `fd63de5c60fc9d19ec643a2c491d5a815581765a`
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
| feed watchdog rebuilt on per-page/per-signal heartbeats plus bounded HL HTTP requests (PR #409 branch, not yet committed/CI-verified) | pending commit on codex/lane3-no-cap-watchdog | `2026-09-24T08:23:07Z` | manual `Claude builder session 2026-09-24: sandbox blocked commit/push/tsc/npm test, code+tests written in working tree only` |
| Lane 3 final source-capture implementation commit | bbc574f | `2026-09-21T19:49:53Z` | commit `bbc574feb7ff31bcc1026e1c3522f4d9887a17aa` |
| builder-validated executor subtests passing | 211 | `2026-09-21T19:49:53Z` | commit `bbc574feb7ff31bcc1026e1c3522f4d9887a17aa` |
| verified Invo trade-feed surfaces | 4 | `2026-09-17T23:46:12Z` | issue `#400` |
| exploratory feed-exposed portfolios missing from current selector | 109 | `2026-09-17T23:46:12Z` | issue `#403` |
| exploratory missing portfolios meeting frozen selector on visible evidence | 53 | `2026-09-17T23:46:12Z` | issue `#403` |
| real trading enabled | False | `2026-09-17T23:10:50Z` | manual `VM runtime env 2026-09-17T23:10Z` |

**Blocker:** Integrated Lane 3 RC implementation 39d53c57f65a4dfae4eac52f60de5ed15d20e30a includes causal source-time admission, admission-index v2, direct-watch state v8, the managed-position sourceBaseShortId write/read schema repair, and bounded atomic-publication generation retention while preserving the reviewed safety invariants. Malformed lifecycle variants fail closed; deployment uses the strict atomic publication reader; and the publication reader now accepts only the exact safe writer generation-id format, rejecting path-segment traversal before report/ledger reads. All prior strict selector/audit, canonical candidate/admission, runtime-health profitability, atomic publication, source capture, funding, replay, direct-watch, dedupe, readiness, reset, and live-off invariants remain intact. The 298/22/18/61 subtest counts above are pinned to 39d53c57 and are now stale: PR #409's codex/lane3-no-cap-watchdog branch has since removed the uncapped-residency count cap and rebuilt the feed watchdog on per-page/per-signal progress heartbeats (fetchFeedBackfill onPage, runSignalBatchBySource onProgress, syncStagedFundingForClose onWait) plus a bounded AbortSignal.timeout on every Hyperliquid info() request, so the watchdog bound no longer scales with page/signal count or a future live topology's larger trader population. The previously recorded 'default direct-watch resident capacity proven by timeout/page/deadline budget = 16' fact has been removed as obsolete: it described a hard admission cap that commit 4d64c74 already removed. This session could not produce fresh exact-SHA executor/contract/pytest counts or a new commit: the sandbox blocked all code-execution and git-mutation commands (npm/tsc/pytest, git add/commit/push) pending approval unavailable in this non-interactive run. Code and new regression tests exist only in the working tree pending commit. Remaining gate before this is treated as validated: someone with shell approval must run `npm run check` in services/invo-notification-executor, run the relevant Python/pytest suite, regenerate this file with `python scripts/render_ai_team_state.py`, commit, push, and re-sync state.json with the real commit SHA and fresh counts. This is not profitability evidence. Production remains fd63de5 and real trading is OFF. Remaining gate: GitHub CI and BOTH independent exact-SHA reviews must PASS before shadow-only deployment.
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
