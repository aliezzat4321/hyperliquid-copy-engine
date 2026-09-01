#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re

from ai_team_orchestrator import (
    ACTIVE_STATUSES,
    DB_PATH,
    REPO,
    RUNTIME_STATUS_ISSUE,
    STATE_ROOT,
    GitHub,
    Ledger,
    RuntimeLedgerFiles,
    assignment_marker,
    load_config,
    parse_task_class,
    route_review,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--target-sha", required=True)
    args = parser.parse_args()

    cfg = load_config()
    gh = GitHub(REPO)
    pr = gh.pr(args.pr)
    if str(pr.get("state")) != "open":
        raise SystemExit("manager review requires open PR")
    branch = str(pr["head"]["ref"])
    head_sha = str(pr["head"]["sha"])
    body = str(pr.get("body") or "")
    if not branch.startswith("manager/"):
        raise SystemExit("manager review requires manager/ branch")
    if "AI_TEAM_MANAGER_PROTECTED=YES" not in body:
        raise SystemExit("manager protected marker missing")
    if head_sha != args.target_sha:
        raise SystemExit(f"stale manager target {args.target_sha} != {head_sha}")

    match = re.search(r"(?mi)^AI_TEAM_MANAGER_ISSUE=(\d+)\s*$", body)
    if not match:
        raise SystemExit("AI_TEAM_MANAGER_ISSUE marker missing")
    issue_number = int(match.group(1))
    issue = gh.issue(issue_number)
    trusted = set(cfg["trusted_author_associations"])
    if str(issue.get("author_association") or "") not in trusted:
        raise SystemExit("manager issue author not trusted")

    task_class, escalation_reason = parse_task_class(str(issue.get("body") or ""))
    model = route_review(cfg, task_class, escalation_reason)
    ledger = Ledger(DB_PATH)

    represented = ledger.db.execute(
        "SELECT id FROM tasks WHERE pr_number=? AND target_sha=? LIMIT 1",
        (args.pr, args.target_sha),
    ).fetchone()
    if represented:
        print(f"MANAGER_REVIEW=ALREADY_REPRESENTED task={represented['id']}")
        return 0

    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    active = ledger.db.execute(
        f"SELECT id FROM tasks WHERE pr_number=? AND status IN ({placeholders}) LIMIT 1",
        (args.pr, *ACTIVE_STATUSES),
    ).fetchone()
    if active:
        print(f"MANAGER_REVIEW=CHAIN_ACTIVE task={active['id']}")
        return 0

    task_id = ledger.create_task(
        issue_number=issue_number,
        pr_number=args.pr,
        task_type="REVIEW",
        agent="CLAUDE",
        model_class=model,
        task_class=task_class,
        target_sha=args.target_sha,
    )
    gh.comment(
        args.pr,
        assignment_marker(
            task_id=task_id,
            agent="CLAUDE",
            task_type="REVIEW",
            model_class=model,
            task_class=task_class,
            issue_number=issue_number,
            pr_number=args.pr,
            target_sha=args.target_sha,
            escalation_reason=escalation_reason,
        ),
    )
    RuntimeLedgerFiles(STATE_ROOT, DB_PATH, REPO, RUNTIME_STATUS_ISSUE).event(
        "TASK_ASSIGNED",
        assignment_id=task_id,
        issue=issue_number,
        pr=args.pr,
        agent="CLAUDE",
        model=model,
        task_type="REVIEW",
        target_sha=args.target_sha,
        result="protected manager PR entered canonical review/repair ledger",
    )
    print(f"MANAGER_REVIEW=QUEUED task={task_id} model={model} pr={args.pr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
