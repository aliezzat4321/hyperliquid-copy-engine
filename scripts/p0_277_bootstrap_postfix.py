#!/usr/bin/env python3
"""Temporary follow-up patcher for #277 test-compatible fail-closed semantics."""
from pathlib import Path

p = Path("scripts/ai_team_orchestrator.py")
s = p.read_text()
old = '''            if str(exc) != "MISSING_COMPLETION_CONTRACT":
                raise
            if reconciled:
'''
new = '''            if str(exc) != "MISSING_COMPLETION_CONTRACT":
                raise
            # Without a canonical issue number there is nothing safe to reconcile or
            # project. Preserve the original fail-closed parser error.
            if issue_number is None:
                raise
            if reconciled:
'''
if s.count(old) != 1:
    raise SystemExit(f"completion-contract anchor mismatch: {s.count(old)}")
s = s.replace(old, new, 1)
p.write_text(s)

t = Path("tests/test_ai_team_orchestrator.py")
tests = t.read_text()
old = '''    team.reconcile_parent_finalizers = lambda: False
    team.sync_runtime_checkpoint = lambda: None
    team.kick_trello_reconciliation = lambda: None

    team.cycle()
'''
new = '''    team.reconcile_parent_finalizers = lambda: False
    team.sync_runtime_checkpoint = lambda: None
    team.kick_trello_reconciliation = lambda: None
    # This test isolates acceptance-evidence failure semantics rather than queue admission.
    team.claim_ready_issue = lambda: False
    team.promote_queued_issue = lambda: False

    team.cycle()
'''
if tests.count(old) != 1:
    raise SystemExit(f"acceptance test anchor mismatch: {tests.count(old)}")
tests = tests.replace(old, new, 1)
old = '''    ledger.create_task(
        issue_number=120, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE",
    )
'''
new = '''    ledger.create_task(
        issue_number=120, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="RUNNING",
    )
'''
if tests.count(old) != 1:
    raise SystemExit(f"active-claim test anchor mismatch: {tests.count(old)}")
tests = tests.replace(old, new, 1)
t.write_text(tests)
