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
