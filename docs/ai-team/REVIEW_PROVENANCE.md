# Review Provenance

## The limitation, stated plainly

Claude and ChatGPT/Codex currently act through **one GitHub account**. Every commit,
pull request, review and merge in this repository carries the same identity.

GitHub therefore **cannot prove** that a review was independent. A "reviewed by the
other agent" claim is an assertion recorded by the agents, not an authenticated fact.
Nothing in this operating system should be read as cryptographic separation of duties.

Do not describe review independence as enforced. It is *recorded*.

## What we record instead

Because the identity cannot be proved, it must at least be explicit and auditable:

- **Logical agent identity** — `CLAUDE` or `CODEX_CHATGPT`, never a GitHub handle.
  Used in `state.json` priorities, the experiment registry, and PR metadata.
- **Reviewed commit SHA** — the exact head the reviewer examined. A review of an
  earlier commit is not a review of the merged one; recording the SHA makes a stale
  review visible instead of invisible.
- **Builder ≠ reviewer** — enforced by `scripts/ai_team_contract.py` for active
  priorities and for any experiment marked `COMPLETE`.

## What CI can and cannot check

| Check | Enforced |
|---|---|
| Builder and reviewer are different logical agents | Yes — contract validator |
| A profitability-critical priority names an AI reviewer | Yes — contract validator |
| A `COMPLETE` experiment names a reviewer and a reviewed commit | Yes — contract validator |
| The named reviewer actually performed the review | **No** |
| The reviewer was a different process/model from the builder | **No** |
| The reviewed commit equals the merged commit | **No** — reviewers must check by eye |

## Improving this later

In rough order of value:

1. Give each agent its own GitHub App or machine account, so authorship and review
   are attributable at the platform level.
2. Enable branch protection requiring at least one approving review from an identity
   other than the author.
3. Require the merge commit to reference the reviewed SHA, so a post-review push is
   detectable rather than silent.

Until (1) exists, treat the reviewer field as a **statement of intent and record of
responsibility**, and rely on the substance of review comments — not on the label.
