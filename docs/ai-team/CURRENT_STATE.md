# Current State

Generated from `docs/ai-team/state.json`. Do not hand-edit.

**Snapshot:** 2026-09-14T23:02:08Z  
**Updated by:** CODEX_CHATGPT  
**Observed main head:** `4653f0d86650d42fea6062ddfd1fcce0ced0f746`  
**Mission:** Maximum sustainable executable risk-aware net profitability across the three Hyperliquid lanes.

## Live trading
**DISABLED** — user authorization: **NO**.

## Active priorities
| Priority | Issue | Objective | Builder | Reviewer | Status | Profit-critical |
|---|---:|---|---|---|---|---|
| P0 | #330 | Complete Lane 3 causal L2 execution and net-cost repair | CODEX_CHATGPT | CLAUDE | IN_REVIEW | yes |
| P0 | #328 | Run integrated Lane 3 pre-Opus challenge after both repair halves | CODEX_CHATGPT | CLAUDE | OPEN | yes |
| P0 | #327 | Challenge Lane 2 repair against every Opus finding | CODEX_CHATGPT | CLAUDE | OPEN | yes |
| P0 | #334 | Restore truthful repository-wide CI baseline | CODEX_CHATGPT | CLAUDE | IN_PROGRESS | no |

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
| Lane 2 Opus-repair PR open | True | `2026-09-14T23:02:08Z` | PR `#318` |
| Lane 2 repair head | d30772140f20cc5a6aa9d8bc9d8f02f372517863 | `2026-09-14T23:02:08Z` | PR `#318` |
| last measured durable verified identities | 26 | `2026-09-11T16:55:20Z` | run `34624217892` |
| real trading enabled | False | `2026-09-14T22:39:38Z` | issue `#130` |

**Blocker:** PR #318 contains the proposed identity-safety and capacity repair but remains open; actual post-repair Lane 2 shadow throughput/runtime proof and an exact-SHA lane-specific Opus PASS are still required.  
**Next:** Issue #312.

## Lane 3 — Direct Invo notification shadow copying
**Status:** `SHADOW_EVIDENCE_MEASUREMENT_INCOMPLETE`

| Fact | Value | Observed | Source |
|---|---:|---|---|
| Lane 3 execution-repair head | 976c5e2364f9f40b45a3b0a56b31339b3c5036bb | `2026-09-14T23:02:08Z` | PR `#332` |
| exact-head Lane 3 executor CI | PASS | `2026-09-14T23:00:30Z` | run `34906771193` |
| exact-head live-sensitive classification guard | PASS | `2026-09-14T23:00:30Z` | run `34906771110` |
| independent second Codex review | RUNNING | `2026-09-14T23:02:08Z` | run `34906695969` |
| real trading enabled | False | `2026-09-14T22:39:38Z` | issue `#130` |

**Blocker:** The #320 restart/backfill repair and #330 execution/funding repair are built, but exact-SHA independent Codex review, integrated challenge, deployment and fresh prospective evidence remain incomplete; no Lane 3 Opus re-audit has passed yet.  
**Next:** Issue #330.

## Infrastructure
**Status:** `DEGRADED`

| Fact | Value | Observed | Source |
|---|---:|---|---|
| owner-authorized market-shadow reset task exists | True | `2026-09-14T17:28:43Z` | issue `#310` |
| previous 100% storage measurement is no longer a current-state proof | True | `2026-09-14T23:02:08Z` | issue `#310` |
| repository CI baseline repair open | True | `2026-09-14T23:02:08Z` | issue `#334` |
| runtime reports unrelated work continuing | True | `2026-09-14T22:39:38Z` | issue `#130` |
| real trading enabled | False | `2026-09-14T22:39:38Z` | issue `#130` |

**Blocker:** Repository-wide CI is currently red because an obsolete Opus runner has isolated Ruff debt and the generated AI-team state expired its 72-hour freshness bound. Normal orchestration also still projects stale work, so Lane 3 critical reviews are running through exact-SHA direct review gates instead of waiting behind the scheduler.  
**Next:** Issue #334.

## Update rule
Every fact above carries its own `observed_at` and source reference. The builder of any PR that materially changes these facts updates `state.json` in the same PR, with provenance. The independent reviewer verifies it. `scripts/render_ai_team_state.py` regenerates this file and CI rejects both drift and a snapshot older than the bound in `scripts/ai_team_contract.py`.
