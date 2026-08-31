# CLAUDE.md

Read `@AGENTS.md` first.

Then read:
- `@docs/ai-team/CURRENT_STATE.md`
- `@docs/ai-team/PROFITABILITY_STANDARD.md`
- the assigned GitHub Issue and only the subsystem docs it links.

Do not recursively re-audit the repository on routine tasks. GitHub `main` is the accepted code source of truth; Issues and PRs are the shared communication layer with Codex/ChatGPT.

For assigned implementation work:
1. confirm no open PR duplicates the task;
2. work on a `claude/...` branch;
3. keep the change scoped to the Issue;
4. run relevant tests and quantify before/after evidence;
5. update durable project state/experiment records when required by `AGENTS.md`;
6. open a PR and request independent Codex/ChatGPT review for profitability-critical work;
7. do not merge your own profitability-critical work unless explicitly authorized.

For review work, assume the claimed improvement may be false and actively check for look-ahead, survivorship bias, multiple testing, incorrect PnL/cost math, execution mismatch, data-integrity failure, capacity limits and unsafe live changes.

Never enable real trading without explicit user authorization for the specific step.