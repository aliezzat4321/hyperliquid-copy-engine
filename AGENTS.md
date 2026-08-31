# AGENTS.md

## Mission
Build the Hyperliquid copy engine toward maximum **sustainable, executable, risk-aware net profitability**. Code volume, backtest PnL, leaderboard rank and gross shadow PnL are not success metrics by themselves.

## Sources of truth
Use this order when facts conflict:
1. `main` for accepted code and policy.
2. Production/runtime observations for current operational state.
3. GitHub Issues for active work ownership and success criteria.
4. Pull requests for proposed implementation and review discussion.
5. `docs/ai-team/state.json` + generated `docs/ai-team/CURRENT_STATE.md` for the compact project snapshot.
   Every fact there carries its own `observed_at` and source reference; trust a fact no
   further than its provenance.
6. `docs/ai-team/experiments/INDEX.md` for accepted research evidence, including failures.
7. Older narrative docs only for background.

Never treat a chat transcript as the durable source of truth.

## Start every task efficiently
1. Read this file.
2. Read `docs/ai-team/CURRENT_STATE.md`.
3. Read the assigned GitHub Issue and its linked docs.
4. Inspect open PRs touching the same subsystem.
5. Check `docs/ai-team/experiments/INDEX.md` before proposing a hypothesis. A recorded
   `FAIL` or `INCONCLUSIVE` is a result; do not silently repeat it.
6. Use `docs/ai-team/SYSTEM_MAP.md` to locate the relevant modules, services and
   workflows. Read only those paths. Do **not** re-audit the whole repository unless the
   Issue explicitly requires it.

## Team model
- One builder owns each Issue.
- The other AI agent is the independent reviewer for profitability-critical work whenever practical.
- Builder and reviewer must not silently collaborate toward agreement: the reviewer should actively try to falsify the claimed improvement.
- GitHub Issues, PRs, review comments, tests and experiment records are the communication channel between agents.

## Branch / PR rules
- Never develop directly on `main`.
- Use `codex/...` or `claude/...` branches.
- One logical objective per PR. Do not mix cleanup, infrastructure and profitability changes unless inseparable.
- PRs must use the repository template and state whether project state, architecture, research evidence or live permissions changed.
- Do not merge your own profitability-critical PR without independent review unless explicitly authorized.

## Profitability truth
Before calling a strategy or slice profitable, follow `docs/ai-team/PROFITABILITY_STANDARD.md`
and gate on the versioned floors in `docs/ai-team/PROMOTION_POLICY.md`. Report the
`policy_version` with any promotion verdict. Never loosen a threshold to make a specific
candidate pass; if a candidate fails, the finding is that it failed.
At minimum report sample size, distinct days, in-sample vs prospective status, gross and execution-cost-adjusted economics, fees, spread/slippage assumptions or measurements, funding where relevant, drawdown, latency, unresolved/open exposure and uncertainty.

Historical discovery != prospective validation != shadow evidence != micro-live evidence != validated live edge.

Do not optimize metrics by weakening data-integrity, identity, execution or statistical gates.

## Causal / statistical rules
- Never use future-known attributes as an entry-time selection rule.
- Freeze hypotheses/rules before prospective windows.
- Account for broad screening / multiple testing when selecting winners.
- Treat unresolved positions and missing outcomes explicitly; do not let them disappear from promotable slices.
- Fail closed on material accounting or data-integrity failures.
- Prefer measured execution costs. Scenario costs must be clearly labelled assumptions.

## Live-trading boundary
Read `docs/ai-team/LIVE_TRADING_GATE.md` before any work that could affect real orders.
A PR touching real-trading permissions, order routing, signing/key handling, live systemd
environment or safety thresholds must declare `LIVE-SENSITIVE: YES` in its description;
`live-sensitive-guard` fails otherwise. That declaration classifies the change — it does
not authorize trading.
- `REAL_TRADING_ENABLED` remains disabled unless the user explicitly authorizes a specific live step.
- Research/shadow findings cannot enable capital by themselves.
- Never weaken live safety controls as a side effect of research work.

## Documentation maintenance contract
The builder updates durable knowledge in the same PR when the task changes it:
- `docs/ai-team/state.json` for current status / priorities / material counts. Every fact
  needs `value`, `observed_at`, `source_type` and `source_ref`; an uncited number is a
  liability, because the next agent will cite it as established.
- `docs/ai-team/DECISIONS.md` for accepted architecture or policy decisions.
- `docs/ai-team/experiments/registry.json` for material quant experiments, then regenerate
  `INDEX.md`.
- `docs/ai-team/SYSTEM_MAP.md` when a component moves, is added or is retired.
- Relevant subsystem docs when interfaces or architecture changed.

The reviewer must check these updates and request them before merge when required.
`CURRENT_STATE.md` and `experiments/INDEX.md` are generated; regenerate them with
`python scripts/render_ai_team_state.py` and never hand-edit them. CI rejects drift, an
out-of-bounds snapshot age, unknown schema fields, placeholder owners, a builder who is
also the reviewer, and any live-trading authorization that is not a complete, unexpired,
user-issued object.

## Definition of done
A task is not done because code exists. It is done when:
- success criteria from the Issue are demonstrated;
- relevant tests pass;
- before/after evidence is reported;
- risks and rollback are documented;
- durable project state is updated if needed;
- independent review comments are resolved;
- the reviewing agent and the reviewed commit SHA are recorded, per
  `docs/ai-team/REVIEW_PROVENANCE.md`;
- no unauthorized real-trading change occurred.
