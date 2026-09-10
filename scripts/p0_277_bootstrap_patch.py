#!/usr/bin/env python3
"""Temporary branch-only patcher for #277. Deleted before the final source commit."""
from pathlib import Path

p = Path("scripts/ai_team_orchestrator.py")
s = p.read_text()

replacements = []

replacements.append((
'''    row = dict(task)
    if str(row.get("failure_class") or "") in RECOVERY_CLASSES:
        return str(row["failure_class"])
    text = str(row.get("last_error") or "").lower()
    task_type = str(row.get("task_type") or "")
    if (
        re.search(r"\\b(owner_auth_required|owner authorization required|auth_required)\\b", text)
        or "protected_action missing/invalid repository authorization" in text
        or "protected ai-control-plane change lost trusted issue author" in text
        or "protected ai-control-plane change lacks ai_team_protected_change=yes" in text
        or "issue author association no longer trusted" in text
        or "claude_auth_required" in text
    ):
        return "OWNER_AUTH_REQUIRED"
''',
'''    row = dict(task)
    persisted = str(row.get("failure_class") or "")
    text = str(row.get("last_error") or "").lower()
    task_type = str(row.get("task_type") or "")
    protected_scope_mismatch = (
        "protected ai-control-plane change lacks ai_team_protected_change=yes" in text
    )
    # Historical false owner classifications must self-correct from canonical error text.
    # Missing protected-scope metadata is internal routing work, not owner authorization.
    if persisted in RECOVERY_CLASSES and not (
        persisted == "OWNER_AUTH_REQUIRED" and protected_scope_mismatch
    ):
        return persisted
    if protected_scope_mismatch:
        return "PROTECTED_PATH_ATTEMPT"
    if (
        re.search(r"\\b(owner_auth_required|owner authorization required|auth_required)\\b", text)
        or "protected_action missing/invalid repository authorization" in text
        or "protected ai-control-plane change lost trusted issue author" in text
        or "issue author association no longer trusted" in text
        or "claude_auth_required" in text
    ):
        return "OWNER_AUTH_REQUIRED"
'''))

replacements.append((
'''    def pending_owner_action(self) -> str | None:
        row = self.db.execute(
            "SELECT last_error FROM tasks WHERE status='BLOCKED' "
            "AND failure_class='OWNER_AUTH_REQUIRED' ORDER BY updated_at LIMIT 1"
        ).fetchone()
        return str(row["last_error"])[:500] if row else None
''',
'''    def pending_owner_action(self) -> str | None:
        # Re-evaluate persisted owner blocks so old classifier bugs cannot keep
        # publishing a false owner-action banner after routing rules are repaired.
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status='BLOCKED' "
            "AND failure_class='OWNER_AUTH_REQUIRED' ORDER BY updated_at"
        ).fetchall()
        for row in rows:
            probe = dict(row)
            probe["failure_class"] = None
            if classify_recovery_failure(probe) == "OWNER_AUTH_REQUIRED":
                return str(row["last_error"] or "")[:500]
        return None
'''))

replacements.append((
'''    def due(self) -> sqlite3.Row | None:
        now = utcnow()
        return self.db.execute(
            """
            SELECT *
              FROM tasks
             WHERE status IN ('PENDING','RETRY','WAITING_RATE_LIMIT','WAITING_CI',
                              'WAITING_EVIDENCE_WINDOW')
               AND (retry_at IS NULL OR retry_at <= ?)
             ORDER BY CASE status WHEN 'WAITING_CI' THEN 0 ELSE 1 END, created_at
             LIMIT 1
            """,
            (now,),
        ).fetchone()
''',
'''    def due(self) -> sqlite3.Row | None:
        now = utcnow()
        return self.db.execute(
            """
            SELECT *
              FROM tasks
             WHERE status IN ('PENDING','RETRY','WAITING_RATE_LIMIT','WAITING_CI',
                              'WAITING_EVIDENCE_WINDOW')
               AND (retry_at IS NULL OR retry_at <= ?)
             ORDER BY
               CASE status
                 WHEN 'WAITING_CI' THEN 0
                 WHEN 'PENDING' THEN 1
                 WHEN 'RETRY' THEN 2
                 WHEN 'WAITING_RATE_LIMIT' THEN 3
                 WHEN 'WAITING_EVIDENCE_WINDOW' THEN 4
                 ELSE 5
               END,
               CASE WHEN status='PENDING' THEN updated_at END DESC,
               CASE WHEN status!='PENDING' THEN COALESCE(retry_at,updated_at,created_at) END ASC,
               created_at DESC
             LIMIT 1
            """,
            (now,),
        ).fetchone()
'''))

replacements.append((
'''    def has_queue_claim_conflict(self) -> bool:
        """Keep one active claim, except for a future provider-capacity wait."""
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = self.db.execute(
            f"SELECT 1 FROM tasks WHERE status IN ({placeholders}) "
            "AND NOT (agent='CLAUDE' AND status='WAITING_RATE_LIMIT' "
            "AND retry_at IS NOT NULL AND retry_at > ?) LIMIT 1",
            (*ACTIVE_STATUSES, utcnow()),
        ).fetchone()
        return bool(row)
''',
'''    def has_queue_claim_conflict(self) -> bool:
        """Only an actually running worker owns the execution claim.

        Pending/retry/provider/CI/evidence waits are task-local durable states and must
        never suppress promotion of unrelated ready lane work.
        """
        row = self.db.execute(
            "SELECT 1 FROM tasks WHERE status='RUNNING' LIMIT 1"
        ).fetchone()
        return bool(row)
'''))

replacements.append((
'''    def claim_ready_issue(self) -> bool:
        label = self.cfg["labels"]["ready"]
        for issue in self.gh.ready_issues(label):
''',
'''    def claim_ready_issue(self) -> bool:
        label = self.cfg["labels"]["ready"]
        ready = self.gh.ready_issues(label)

        def ready_key(issue: dict[str, Any]) -> tuple[int, int]:
            metadata = queue_metadata(str(issue.get("body") or ""))
            priority = metadata[0] if metadata else 0
            return priority, int(issue["number"])

        for issue in sorted(ready, key=ready_key):
'''))

replacements.append((
'''        self.migrate_legacy_remediation()
        self.reconcile_recovery()
        self.reconcile_completion_rollout()
        self.reconcile_handoffs()
        if self.ledger.due() is not None:
            self.reconcile_parent_finalizers()
        self.sync_runtime_checkpoint()
        self.kick_trello_reconciliation()
        task = self.ledger.due()
        if task is None:
            if not self.claim_ready_issue():
                self.reconcile_parent_finalizers()
                if not self.claim_ready_issue():
                    self.promote_queued_issue()
            task = self.ledger.due()
''',
'''        self.migrate_legacy_remediation()
        self.reconcile_recovery()
        self.reconcile_completion_rollout()
        self.reconcile_handoffs()
        self.reconcile_parent_finalizers()
        self.sync_runtime_checkpoint()
        self.kick_trello_reconciliation()
        # Work-conserving invariant: waiting/retrying work never owns the global queue.
        # Admit one READY/queued issue each cycle before choosing the next due action.
        if not self.claim_ready_issue():
            self.promote_queued_issue()
        task = self.ledger.due()
'''))

replacements.append((
'''                self.gh.remove_label(number, self.cfg["labels"]["blocked"])
                self.gh.remove_label(number, self.cfg["labels"]["ready"])
                self.gh.remove_label(number, self.cfg["labels"]["queued"])
                self.gh.add_labels(number, [self.cfg["labels"]["pending"]])
''',
'''                self.gh.remove_label(number, self.cfg["labels"]["blocked"])
                self.gh.remove_label(number, self.cfg["labels"]["ready"])
                self.gh.remove_label(number, self.cfg["labels"]["queued"])
                self.gh.remove_label(number, self.cfg["labels"]["running"])
                self.gh.add_labels(number, [self.cfg["labels"]["pending"]])
'''))

replacements.append((
'''            self.gh.remove_label(number, self.cfg["labels"]["pending"])
            self.gh.remove_label(number, self.cfg["labels"]["ready"])
            self.gh.remove_label(number, self.cfg["labels"]["queued"])
            self.gh.comment(
''',
'''            self.gh.remove_label(number, self.cfg["labels"]["pending"])
            self.gh.remove_label(number, self.cfg["labels"]["ready"])
            self.gh.remove_label(number, self.cfg["labels"]["queued"])
            self.gh.remove_label(number, self.cfg["labels"]["running"])
            self.gh.comment(
'''))

replacements.append((
'''                AND target_sha IS NOT NULL
             ) OR (
''',
'''                AND target_sha IS NOT NULL
                AND last_error IS NULL
             ) OR (
'''))

replacements.append((
'''        if task is None:
            return
        try:
            if task["status"] == "WAITING_CI":
''',
'''        if task is None:
            return
        try:
            issue_state = self.gh.issue(int(task["issue_number"]))
            if str(issue_state.get("state") or "open").lower() == "closed":
                self.reap_stale_child(task)
                self.ledger.update(
                    task["id"], status="DONE", retry_at=None, systemd_unit=None,
                    last_error="OBSOLETE_CLOSED_ISSUE", failure_class=None,
                    lifecycle_phase="OBSOLETE",
                    next_action="retired because canonical GitHub issue is closed",
                )
                self.runtime.event(
                    "OBSOLETE_CLOSED_TASK_RETIRED", assignment_id=task["id"],
                    issue=task["issue_number"], pr=task["pr_number"],
                    task_type=task["task_type"], status="DONE",
                    unrelated_work_continuing=True,
                )
                return
            if task["status"] == "WAITING_CI":
'''))

for i, (old, new) in enumerate(replacements, start=1):
    count = s.count(old)
    if count != 1:
        raise SystemExit(f"source anchor {i} mismatch: found {count}")
    s = s.replace(old, new, 1)

p.write_text(s)

t = Path("tests/test_ai_team_orchestrator.py")
tests = t.read_text()
marker = "def test_p0_277_scheduler_reconciliation_regressions("
if marker not in tests:
    tests += '''


def test_p0_277_scheduler_reconciliation_regressions(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    false_owner = ledger.create_task(
        issue_number=91, task_type="WAITING_CI", agent="MANAGER", model_class="NONE",
        task_class="ROUTINE", status="BLOCKED", failure_class="OWNER_AUTH_REQUIRED",
        last_error="protected AI-control-plane change lacks AI_TEAM_PROTECTED_CHANGE=YES",
    )
    assert orch.classify_recovery_failure(ledger.get(false_owner)) == "PROTECTED_PATH_ATTEMPT"
    assert ledger.pending_owner_action() is None

    retry = ledger.create_task(
        issue_number=146, task_type="PRODUCTION_VALIDATION", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="RETRY",
        retry_at=orch.utcnow(), last_error="awaiting deterministic phase runner evidence",
    )
    pending = ledger.create_task(
        issue_number=277, task_type="BUILD", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="PENDING",
    )
    assert ledger.due()["id"] == pending
    assert ledger.has_queue_claim_conflict() is False
    ledger.update(pending, status="RUNNING")
    assert ledger.has_queue_claim_conflict() is True
    ledger.update(pending, status="DONE")
    assert ledger.get(retry)["status"] == "RETRY"


def test_p0_277_due_closed_issue_is_retired_without_execution(tmp_path):
    ledger = orch.Ledger(tmp_path / "ledger.sqlite3")
    task_id = ledger.create_task(
        issue_number=146, task_type="PRODUCTION_VALIDATION", agent="CODEX_CHATGPT",
        model_class="CODEX_DEFAULT", task_class="ROUTINE", status="RETRY",
        retry_at=orch.utcnow(), last_error="awaiting deterministic phase runner evidence",
    )

    class GH:
        def issue(self, number):
            assert number == 146
            return {"number": 146, "state": "closed"}

    class Runtime:
        def event(self, *args, **kwargs):
            pass

    team = object.__new__(orch.Orchestrator)
    team.ledger, team.gh, team.runtime = ledger, GH(), Runtime()
    team.reap_stale_child = lambda task: None
    team.sync_runtime_checkpoint = lambda: True
    team.kick_trello_reconciliation = lambda: None
    team.migrate_legacy_remediation = lambda: None
    team.reconcile_recovery = lambda: None
    team.reconcile_completion_rollout = lambda: None
    team.reconcile_handoffs = lambda: None
    team.reconcile_parent_finalizers = lambda: False
    team.claim_ready_issue = lambda: False
    team.promote_queued_issue = lambda: False
    team.cycle()
    retired = ledger.get(task_id)
    assert retired["status"] == "DONE"
    assert retired["last_error"] == "OBSOLETE_CLOSED_ISSUE"
    assert retired["lifecycle_phase"] == "OBSOLETE"
    assert ledger.due() is None
'''

t.write_text(tests)
