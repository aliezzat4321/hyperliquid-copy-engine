# AI Team Operating System

This directory is the compact durable memory and governance layer shared by ChatGPT/Codex and Claude.

## Ownership
- **Builder agent:** owns the Issue, code, tests, before/after evidence, and required state/experiment updates.
- **Reviewer agent:** independently challenges profitability, safety and correctness claims and blocks merge if durable state is stale.
- **ChatGPT/Codex lead:** default maintainer of coordination state, production integration and cross-lane prioritization.
- **Claude:** default independent quant/engineering challenger; may be builder when explicitly assigned.
- **User:** sole authority for capital deployment / live-trading authorization.

## Files

| File | Kind | Purpose |
|---|---|---|
| `state.json` | hand-maintained | Compact project snapshot; every fact carries provenance |
| `CURRENT_STATE.md` | generated | Human view of `state.json` |
| `SYSTEM_MAP.md` | rarely changing | Where each lane's code, services, stores and workflows live |
| `PROFITABILITY_STANDARD.md` | policy | What must be reported before calling anything profitable |
| `PROMOTION_POLICY.md` + `.json` | versioned policy | The numeric floors a slice must clear |
| `LIVE_TRADING_GATE.md` | policy | Structured capital authorization; user-only |
| `REVIEW_PROVENANCE.md` | policy | What review independence does and does not prove |
| `DECISIONS.md` | append-only | Accepted architecture/policy decisions |
| `experiments/registry.json` | hand-maintained | Machine-readable experiment record |
| `experiments/INDEX.md` | generated | Human view; check before proposing a hypothesis |
| `profitability_scoreboard.json` | hand-maintained | Issue #141 three-lane executable-edge decision record |
| `PROFITABILITY_SCOREBOARD.md` | generated | Human view of the three-lane scoreboard |

Regenerate `CURRENT_STATE.md` and `experiments/INDEX.md` with
`python scripts/render_ai_team_state.py`. Regenerate `PROFITABILITY_SCOREBOARD.md`
with `python scripts/render_profitability_scoreboard.py`.
Validate everything with `python scripts/validate_ai_team_contract.py`.

The active Issue #141 builder owns refreshing the hand-maintained
`profitability_scoreboard.json`; after Issue #141 closes, ownership returns to the
ChatGPT/Codex lead. Before its 72-hour freshness bound expires, the owner must update
`as_of` and any changed evidence with current source references and observation times,
run `python scripts/render_profitability_scoreboard.py`, and run
`pytest -q tests/test_profitability_scoreboard_v1.py`. Do not advance `as_of` without
rechecking the underlying evidence.

## What changes often
`state.json` and `experiments/registry.json` are the manually updated records.
`CURRENT_STATE.md` and `experiments/INDEX.md` are generated from them.

Update `state.json` in the same PR whenever a change materially alters a lane status, blocker, priority, accepted profitability evidence, runtime state used for decisions, or live-readiness status.

## What changes rarely
`AGENTS.md`, `CLAUDE.md`, `SYSTEM_MAP.md`, `PROFITABILITY_STANDARD.md`,
`PROMOTION_POLICY.md`, `LIVE_TRADING_GATE.md` and `REVIEW_PROVENANCE.md` are policy.
Change them only through a dedicated reviewed governance PR.

## Research memory
Material experiments get one file under `experiments/` using `TEMPLATE.md`. Record failures and inconclusive results too, so agents do not repeat work.

## Decisions
Append accepted architecture/policy decisions to `DECISIONS.md`. Do not rewrite history; supersede an old decision with a new dated entry.

## Workflow
Issue -> one builder -> branch -> PR/evidence -> other agent review -> CI -> merge -> state/experiment record already included in PR.

Chats are temporary. GitHub is the team memory.
