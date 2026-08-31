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
6. Experiment records for accepted research evidence.
7. Older narrative docs only for background.

Never treat a chat transcript as the durable source of truth.

## Start every task efficiently
1. Read this file.
2. Read `docs/ai-team/CURRENT_STATE.md`.
3. Read the assigned GitHub Issue and its linked docs.
4. Inspect open PRs touching the same subsystem.
5. Read only the relevant code paths. Do **not** re-audit the whole repository unless the Issue explicitly requires it.

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
Before calling a strategy or slice profitable, follow `docs/ai-team/PROFITABILITY_STANDARD.md`.
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
- `REAL_TRADING_ENABLED` remains disabled unless the user explicitly authorizes a specific live step.
- Research/shadow findings cannot enable capital by themselves.
- Never weaken live safety controls as a side effect of research work.

## Documentation maintenance contract
The builder updates durable knowledge in the same PR when the task changes it:
- `docs/ai-team/state.json` for current status / priorities / material counts.
- `docs/ai-team/DECISIONS.md` for accepted architecture or policy decisions.
- `docs/ai-team/experiments/` for material quant experiments.
- Relevant subsystem docs when interfaces or architecture changed.

The reviewer must check these updates and request them before merge when required.
`CURRENT_STATE.md` is generated from `state.json`; do not hand-edit it.

## Definition of done
A task is not done because code exists. It is done when:
- success criteria from the Issue are demonstrated;
- relevant tests pass;
- before/after evidence is reported;
- risks and rollback are documented;
- durable project state is updated if needed;
- independent review comments are resolved;
- no unauthorized real-trading change occurred.
