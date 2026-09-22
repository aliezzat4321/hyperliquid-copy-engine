# Current State

Generated from `docs/ai-team/state.json`. Do not hand-edit.

**Snapshot:** 2026-09-22T09:49:21Z
**Updated by:** CODEX_CHATGPT
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
| integrated RC live authorization / real trading | authorization NONE / REAL_TRADING_ENABLED=NO | `2026-09-22T09:49:21Z` | commit `1fd7f8131445e602ed650331caeb9d83dc1979d6` |
| integrated RC reset, deploy-contract, and shadow-health pytest tests passing | 17 | `2026-09-22T09:49:21Z` | commit `1fd7f8131445e602ed650331caeb9d83dc1979d6` |
| integrated RC focused non-funding admission/discovery/durability/projector/readiness subtests passing | 73 | `2026-09-22T09:49:21Z` | commit `1fd7f8131445e602ed650331caeb9d83dc1979d6` |
| integrated RC focused funding subtests passing | 28 | `2026-09-22T09:49:21Z` | commit `1fd7f8131445e602ed650331caeb9d83dc1979d6` |
| integrated RC full executor subtests passing | 265 | `2026-09-22T09:49:21Z` | commit `1fd7f8131445e602ed650331caeb9d83dc1979d6` |
| integrated Lane 3 release-candidate implementation commit | 1fd7f8131445e602ed650331caeb9d83dc1979d6 | `2026-09-22T09:49:21Z` | commit `1fd7f8131445e602ed650331caeb9d83dc1979d6` |
| Lane 3 final source-capture implementation commit | bbc574f | `2026-09-21T19:49:53Z` | commit `bbc574feb7ff31bcc1026e1c3522f4d9887a17aa` |
| builder-validated executor subtests passing | 211 | `2026-09-21T19:49:53Z` | commit `bbc574feb7ff31bcc1026e1c3522f4d9887a17aa` |
| default direct-watch resident capacity proven by timeout/page/deadline budget | 16 | `2026-09-20T15:33:55Z` | commit `67efba6` |
| verified Invo trade-feed surfaces | 4 | `2026-09-17T23:46:12Z` | issue `#400` |
| exploratory feed-exposed portfolios missing from current selector | 109 | `2026-09-17T23:46:12Z` | issue `#403` |
| exploratory missing portfolios meeting frozen selector on visible evidence | 53 | `2026-09-17T23:46:12Z` | issue `#403` |
| real trading enabled | False | `2026-09-17T23:10:50Z` | manual `VM runtime env 2026-09-17T23:10Z` |

**Blocker:** Integrated Lane 3 RC implementation 1fd7f8131445e602ed650331caeb9d83dc1979d6 is frozen after closing all findings from the prior integrated Codex+Opus review, including causal selector archive projection, unknown/unresolved open economics, explicit incomplete-close denominators, integrated deployment health, transient admission-index corruption, corrupt funding evidence, reset hygiene, feed-evidence causal boundary/path, cross-surface dedupe, and unresolved runtime exposure. Independent isolated validation passes 265/265 full executor subtests, 28/28 focused funding, 73/73 focused integrated regressions, 17/17 reset/deploy/health pytest tests, AI-team validator, 61/61 contract tests, git diff check, and deploy shell syntax. Production remains fd63de5 and real trading is OFF. Remaining gate: GitHub CI and BOTH independent exact-SHA reviews must PASS the final RC head before shadow-only deployment and prospective source/event-recall proof.
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
