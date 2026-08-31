# Review Provenance

## The limitation, stated plainly

Claude and ChatGPT/Codex currently act through **one GitHub account**. Every commit,
pull request, review and merge in this repository carries the same identity.

GitHub therefore **cannot prove** that a review was independent. A "reviewed by the
other agent" claim is an assertion recorded by the agents, not an authenticated fact.
Nothing in this operating system should be read as cryptographic separation of duties.

Do not describe review independence as enforced. It is *recorded*.

This is not a theoretical limit. On 2026-08-31 Codex/ChatGPT attempted a formal
`REQUEST_CHANGES` review on PR #95 and GitHub refused it:

> Review Can not request changes on your own pull request

So the shared identity does not merely fail to *prove* independence — it actively
**blocks the formal review states entirely**. Neither agent can `APPROVE` or
`REQUEST_CHANGES` on work the other authored, because GitHub sees one author.

Practical consequence: substantive review between agents happens in **pull request
comments**, and a blocking objection is a comment that says so plus the reviewer
declining to merge. Do not wait for a red "changes requested" badge; it cannot appear.

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
| A formal APPROVE / REQUEST_CHANGES between the two agents | **No** — GitHub refuses it outright |

## Improving this later

In rough order of value:

1. Give each agent its own GitHub App or machine account, so authorship and review
   are attributable at the platform level. This is the only change that unblocks the
   formal review states; everything below depends on it.
2. Enable branch protection requiring at least one approving review from an identity
   other than the author.
3. Require the merge commit to reference the reviewed SHA, so a post-review push is
   detectable rather than silent.

Until (1) exists, treat the reviewer field as a **statement of intent and record of
responsibility**, and rely on the substance of review comments — not on the label.
