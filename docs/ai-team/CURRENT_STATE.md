# Current State

Generated from `docs/ai-team/state.json`. Do not hand-edit.

**Snapshot:** 2026-09-03T17:14:49Z  
**Updated by:** CODEX_CHATGPT  
**Observed main head:** `e341451361f5327740ded688ff803dc78e3afe9a`  
**Mission:** Maximum sustainable executable risk-aware net profitability across the three Hyperliquid lanes.

## Live trading
**DISABLED** — user authorization: **NO**.

## Active priorities
| Priority | Issue | Objective | Builder | Reviewer | Status | Profit-critical |
|---|---:|---|---|---|---|---|
| P0 | #90 | Restore market-data capture and storage headroom | CODEX_CHATGPT | CLAUDE | OPEN | yes |
| P0 | #91 | Build Lane 3 notification-edge v2 profitability gate | CLAUDE | CODEX_CHATGPT | OPEN | yes |
| P0 | #92 | Validate and land builder-first Invo wallet resolver | CODEX_CHATGPT | CLAUDE | OPEN | yes |
| P1 | #93 | Automate Lane 1 research-to-prospective validation handoff | CODEX_CHATGPT | CLAUDE | OPEN | yes |

## Lane 1 — Hyperliquid native discovery and prospective copying research
**Status:** `RESEARCH_ACTIVE_HANDOFF_MANUAL`

| Fact | Value | Observed | Source |
|---|---:|---|---|
| screened cohorts | 1,551 | `2026-08-31T07:37:45Z` | run `33369211976` |
| positive screens | 284 | `2026-08-31T07:37:45Z` | run `33369211976` |
| confirmation rows | 800 | `2026-08-31T07:37:45Z` | run `33369211976` |
| robust candidates | 186 | `2026-08-31T07:37:45Z` | run `33369211976` |
| realized slice rows | 26,238 | `2026-08-31T07:37:45Z` | run `33369211976` |
| hard-coded prospective wallet-by-coin targets | 4 | `2026-08-31T11:40:40Z` | run `33388075503` |
| frozen targets approved | 2 | `2026-08-31T11:40:40Z` | run `33388075503` |
| frozen targets with zero observed events | 1 | `2026-08-31T11:40:40Z` | run `33388075503` |

**Blocker:** The broad research funnel (1551 screened cohorts) does not feed the frozen prospective queue, which remains a hard-coded four-row constant in scripts/prospective_champion_lane.py.  
**Next:** Issue #93.

## Lane 2 — Third-party identity resolution
**Status:** `ZERO_CURRENT_PUBLICATION_YIELD`

| Fact | Value | Observed | Source |
|---|---:|---|---|
| candidate portfolios discovered | 324 | `2026-08-31T11:40:40Z` | run `33388075503` |
| portfolios resolution-ready | 174 | `2026-08-31T11:40:40Z` | run `33388075503` |
| published verified identities | 0 | `2026-08-31T11:40:40Z` | run `33388075503` |
| tracked third-party wallets | 12 | `2026-08-31T11:40:40Z` | run `33388075503` |
| third-party prospective events | 610 | `2026-08-31T11:40:40Z` | run `33388075503` |
| builder-first resolver PR state | OPEN_DRAFT_HEAD_PREDATES_LINT_BASELINE_NEEDS_REBASE | `2026-08-31T12:32:51Z` | PR `#86` |

**Blocker:** The Tier B identity gate has published zero identities against 174 resolution-ready portfolios; throughput is not the limit, the seven-clause conjunction is. The builder-first path in PR #86 is unvalidated and its head predates the repository lint baseline, so its last CI result no longer reflects its true state.  
**Next:** Issue #92.

## Lane 3 — Direct Invo notification shadow copying
**Status:** `SHADOW_EVIDENCE_MEASUREMENT_INCOMPLETE`

| Fact | Value | Observed | Source |
|---|---:|---|---|
| shadow opens | 55 | `2026-08-31T09:11:47Z` | run `33376459723` |
| unique shadow closes | 49 | `2026-08-31T09:11:47Z` | run `33376459723` |
| unresolved open shadow positions | 6 | `2026-08-31T09:11:47Z` | run `33376459723` |
| gross mid-to-mid shadow PnL (NOT net; no fee, spread, impact or funding) | $37.771087 | `2026-08-31T09:11:47Z` | run `33376459723` |
| median gross return per closed trade | 27.4256 bps | `2026-08-31T09:11:47Z` | run `33376459723` |
| median open detection latency | 11,032 | `2026-08-31T09:11:47Z` | run `33376459723` |
| signals rejected by the 25s freshness gate | 41 | `2026-08-31T09:11:47Z` | run `33376459723` |
| net-edge attribution engine PR state | OPEN_FOR_REVIEW_CI_GREEN_MERGEABLE | `2026-08-31T11:26:40Z` | PR `#95` |

**Blocker:** No accepted execution-cost-adjusted, prospective-safe profitability gate. The headline PnL above is gross mid-to-mid and survivorship-biased: the 6 open positions never emit a close and are absent from it.  
**Next:** Issue #91.

## Infrastructure
**Status:** `P0_STORAGE_PRESSURE`

| Fact | Value | Observed | Source |
|---|---:|---|---|
| /mnt/HC_Volume_106576526 usage | 100.0% | `2026-08-31T11:40:40Z` | run `33388075503` |
| /mnt/HC_Volume_106576526 bytes available | 0 | `2026-08-31T07:37:45Z` | run `33369211976` |
| root filesystem usage | 71.3% | `2026-08-31T11:40:40Z` | run `33388075503` |

**Blocker:** The data volume is full and the storage guard cannot recover it: the guard stops only hyperliquid-market-capture, nothing prunes the data mount, and the 78% resume threshold is therefore unreachable.  
**Next:** Issue #90.

## Update rule
Every fact above carries its own `observed_at` and source reference. The builder of any PR that materially changes these facts updates `state.json` in the same PR, with provenance. The independent reviewer verifies it. `scripts/render_ai_team_state.py` regenerates this file and CI rejects both drift and a snapshot older than the bound in `scripts/ai_team_contract.py`.
