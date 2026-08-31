# CLAUDE.md

Read `@AGENTS.md` first.

Then read:
- `@docs/ai-team/CURRENT_STATE.md`
- `@docs/ai-team/PROFITABILITY_STANDARD.md`
- the assigned GitHub Issue and only the subsystem docs it links.

Locate code through `docs/ai-team/SYSTEM_MAP.md` rather than by searching the tree.
Check `docs/ai-team/experiments/INDEX.md` before proposing a hypothesis.
Gate promotions on `docs/ai-team/PROMOTION_POLICY.md`, and cite its `policy_version`.

Do not recursively re-audit the repository on routine tasks. GitHub `main` is the accepted code source of truth; Issues and PRs are the shared communication layer with Codex/ChatGPT.

For assigned implementation work:
1. confirm no open PR duplicates the task;
2. work on a `claude/...` branch;
3. keep the change scoped to the Issue;
4. run relevant tests and quantify before/after evidence;
5. update durable project state/experiment records when required by `AGENTS.md`, giving
   every state fact an `observed_at` and a source reference;
6. open a PR and request independent Codex/ChatGPT review for profitability-critical work,
   recording the logical agent identities and the reviewed commit SHA;
7. do not merge your own profitability-critical work unless explicitly authorized.

For review work, assume the claimed improvement may be false and actively check for look-ahead, survivorship bias, multiple testing, incorrect PnL/cost math, execution mismatch, data-integrity failure, capacity limits and unsafe live changes.

Never enable real trading without explicit user authorization for the specific step, and
never write, infer or extend the `live_trading.authorization` object in `state.json`. If a
change touches live permissions, routing, keys, live service environment or safety
thresholds, declare `LIVE-SENSITIVE: YES` in the PR description.
