# AI Team Operating System

This directory is the compact durable memory and governance layer shared by ChatGPT/Codex and Claude.

## Ownership
- **Builder agent:** owns the Issue, code, tests, before/after evidence, and required state/experiment updates.
- **Reviewer agent:** independently challenges profitability, safety and correctness claims and blocks merge if durable state is stale.
- **ChatGPT/Codex lead:** default maintainer of coordination state, production integration and cross-lane prioritization.
- **Claude:** default independent quant/engineering challenger; may be builder when explicitly assigned.
- **User:** sole authority for capital deployment / live-trading authorization.

## What changes often
`state.json` is the only manually updated compact project snapshot. `CURRENT_STATE.md` is generated from it.

Update `state.json` in the same PR whenever a change materially alters a lane status, blocker, priority, accepted profitability evidence, runtime state used for decisions, or live-readiness status.

## What changes rarely
`AGENTS.md`, `CLAUDE.md`, `PROFITABILITY_STANDARD.md` and `LIVE_TRADING_GATE.md` are policy. Change them only through a dedicated reviewed governance PR.

## Research memory
Material experiments get one file under `experiments/` using `TEMPLATE.md`. Record failures and inconclusive results too, so agents do not repeat work.

## Decisions
Append accepted architecture/policy decisions to `DECISIONS.md`. Do not rewrite history; supersede an old decision with a new dated entry.

## Workflow
Issue -> one builder -> branch -> PR/evidence -> other agent review -> CI -> merge -> state/experiment record already included in PR.

Chats are temporary. GitHub is the team memory.