# Engineering Manager Review Policy

This repository uses an independent manager-review gate for every pull request before merge.

## Roles

The implementation agent owns building the change and proving it with tests.

The manager-review agent must be independent from the implementation pass. It is not a style checker. Its job is to challenge whether the change should merge at all.

## Mandatory review questions

For every PR, review the full diff in repository context and answer all of the following:

1. **Problem fit** — Does the implementation actually solve the stated user/business problem, including the real acceptance criterion, or only a narrower proxy?
2. **Solution quality** — Is this the simplest robust architecture? Identify materially better, faster, cheaper, more reliable, or less complex alternatives before approving.
3. **Correctness** — Check assumptions, invariants, state transitions, numerical logic, time handling, data normalization, concurrency, retries, failure modes, and boundary cases.
4. **Trading-system safety** — Fail closed on ambiguous data. No hidden look-ahead, survivorship leakage, retrospective contamination, silent data drops, accidental live-trading promotion, or risk-control bypasses.
5. **Data provenance** — Verify that data sources, timestamps, identity claims, and transformations are auditable and that confidence is not overstated.
6. **Performance and cost** — Look for unnecessary scans, N+1 requests, unbounded memory, expensive APIs, avoidable network traffic, and scaling cliffs.
7. **Security** — Check secrets, untrusted input, shell/process use, path handling, dependency risk, privilege boundaries, and unsafe external requests.
8. **Maintainability** — Prefer generic reusable interfaces over source-specific hacks. Avoid duplication, brittle coupling, dead code, hidden constants, and needless abstraction.
9. **Compatibility** — Check existing CLI/API behavior, persisted schemas, service units, config defaults, migrations, and backward compatibility.
10. **Tests** — Require tests that would fail for the important bugs, not merely line coverage. Include negative cases and at least one realistic end-to-end acceptance path when practical.
11. **Observability** — Important failures and decisions must be diagnosable from logs/reports without reading source code.
12. **Evidence** — Do not approve claims such as VERIFIED, profitable, free, production-ready, or live-safe unless the PR provides evidence matching that claim.

## Review verdict

The manager reviewer must end with exactly one verdict:

- `MANAGER_APPROVED` — no blocking issue remains and the solution is appropriate to merge.
- `MANAGER_CHANGES_REQUIRED` — at least one blocking correctness, architecture, safety, evidence, performance, or maintainability issue remains.

A review with unresolved P0/P1/P2 findings is `MANAGER_CHANGES_REQUIRED`.

## Severity

- **P0** — catastrophic: live trading/risk breach, security compromise, destructive data corruption, or materially false production result.
- **P1** — major correctness or architecture defect; must fix before merge.
- **P2** — meaningful reliability, performance, testing, or maintainability defect; must fix before merge unless explicitly waived with rationale.
- **P3** — non-blocking improvement.

## Merge rule

No feature or fix should be merged until:

1. CI is green.
2. The independent manager review is complete on the current head commit.
3. The verdict is `MANAGER_APPROVED`.
4. Any blocking review findings are resolved and, after material changes, the manager review is rerun.

The manager reviewer must review the latest commit, not an obsolete diff.
