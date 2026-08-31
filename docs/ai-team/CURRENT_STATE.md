# Current State

Generated from `docs/ai-team/state.json`. Do not hand-edit.

**Snapshot:** 2026-08-31T10:46:53Z  
**Observed main head:** `26cc8805a55685e236870b89ff2e6a8c8e889d55`  
**Mission:** Maximum sustainable executable risk-aware net profitability across the three Hyperliquid lanes.

## Live trading
**DISABLED** — user authorization: **NO**.

## Active priorities
| Priority | Issue | Objective | Builder | Reviewer | Status |
|---|---:|---|---|---|---|
| P0 | #90 | Restore market-data capture and storage headroom | Codex/ChatGPT | Claude | OPEN |
| P0 | #91 | Build Lane 3 notification-edge v2 profitability gate | Claude | Codex/ChatGPT | OPEN |
| P0 | #92 | Validate and land builder-first Invo wallet resolver | Codex/ChatGPT | Claude | OPEN |
| P1 | #93 | Automate Lane 1 research-to-prospective validation handoff | UNASSIGNED_ONE_BUILDER | OTHER_AI_AGENT | OPEN |

## Lane 1 — Hyperliquid native discovery and prospective copying research
**Status:** `RESEARCH_ACTIVE_HANDOFF_MANUAL`

Facts: 1551 screened cohorts; 284 positive screens; 800 confirmation rows; 186 robust candidates; 4 hard-coded prospective wallet-by-coin targets; 2 targets approved in latest observed frozen report.

**Blocker:** Existing broad research funnel is not automatically feeding a frozen prospective challenger queue.  
**Next:** Issue #93.

## Lane 2 — Third-party identity resolution
**Status:** `ZERO_CURRENT_PUBLICATION_YIELD`

Facts: about 174 portfolios observed resolution-ready; 0 currently published identity rows; PR #86 builder-first resolver open/draft/mergeable.

**Blocker:** Active identity publication gate yields no current identities; builder-first path requires independent validation.  
**Next:** Issue #92.

## Lane 3 — Direct Invo notification shadow copying
**Status:** `SHADOW_EVIDENCE_MEASUREMENT_INCOMPLETE`

Facts: 55 opens; 49 unique closes; 6 open positions; +$37.77 gross mid-to-mid PnL at latest observed probe; v1 notification-edge patch is not merge-ready.

**Blocker:** No accepted execution-cost-adjusted, prospective-safe profitability gate yet.  
**Next:** Issue #91.

## Infrastructure
`/mnt/HC_Volume_106576526` was observed at **100.0%** usage at `2026-08-31T07:37:45Z`.

**Status:** `P0_STORAGE_PRESSURE`  
**Next:** Issue #90.

## Update rule
The builder of any PR that materially changes these facts updates `state.json` in the same PR. The independent reviewer verifies it. `scripts/render_ai_team_state.py` regenerates this file and CI rejects drift.
