#!/usr/bin/env python3
from pathlib import Path

path = Path("scripts/ai_team_orchestrator.py")
text = path.read_text()
old = '''        prompt = self.review_prompt(pr, task, changed, pr_comments, blockers)
        log_path = CLAUDE_LOG / f"{task['id']}-attempt-{task['attempt']}.json"
'''
new = '''        prompt = self.review_prompt(pr, task, changed, pr_comments, blockers)
        if (
            acceptance_flag(str(issue.get("body") or ""), "AI_TEAM_TEST_FORCE_FIRST_REVIEW_FAIL")
            and not task["previous_sha"]
        ):
            prompt += """

ACCEPTANCE FAULT INJECTION — documentation-only orchestration test:
This is the first review of an explicitly flagged acceptance-test Issue. Even if the
implementation is otherwise correct, return VERDICT=FAIL with exactly one blocker:
`Change the documentation-only acceptance marker REPAIR_STAGE=INITIAL to
REPAIR_STAGE=REPAIRED.` Do not invent any other blocker. This fault injection must
not run on re-review because previous_sha is then populated.
"""
        log_path = CLAUDE_LOG / f"{task['id']}-attempt-{task['attempt']}.json"
'''
if text.count(old) != 1:
    raise SystemExit(f"review-fail anchor count={text.count(old)}")
path.write_text(text.replace(old, new, 1))
print("ISSUE129_REVIEW_FAIL_PATCH=APPLIED")
