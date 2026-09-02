#!/usr/bin/env python3
from pathlib import Path

p = Path("scripts/ai_team_orchestrator.py")
text = p.read_text(encoding="utf-8")

old = """            UPDATE tasks
               SET status='RETRY',
                   retry_at=?,
                   last_error=COALESCE(last_error,'orchestrator restarted during task'),
                   updated_at=?
             WHERE status='RUNNING'
"""
new = """            UPDATE tasks
               SET status='RETRY',
                   retry_at=?,
                   systemd_unit=NULL,
                   last_error=COALESCE(last_error,'orchestrator restarted during task'),
                   updated_at=?
             WHERE status='RUNNING'
"""
if old not in text:
    raise SystemExit("recover_interrupted block not found")
text = text.replace(old, new, 1)

anchor = """    def due(self) -> sqlite3.Row | None:
"""
methods = '''    def child(
        self, parent_id: str, task_type: str, target_sha: str | None = None
    ) -> sqlite3.Row | None:
        sql = "SELECT * FROM tasks WHERE parent_id=? AND task_type=?"
        params: list[Any] = [parent_id, task_type]
        if target_sha is not None:
            sql += " AND target_sha=?"
            params.append(target_sha)
        sql += " ORDER BY created_at LIMIT 1"
        return self.db.execute(sql, params).fetchone()

    def handoff_candidates(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT * FROM tasks
             WHERE (
                    status='DONE'
                AND task_type IN ('BUILD','REPAIR')
                AND pr_number IS NOT NULL
                AND target_sha IS NOT NULL
             ) OR (
                    status='DONE'
                AND task_type='REVIEW'
                AND last_error IN ('review FAIL','CI failure queued for autonomous repair')
             ) OR (
                    status='STALE'
                AND task_type='REVIEW'
                AND (
                       last_error LIKE 'PR moved to %'
                    OR last_error='PR changed during review'
                )
             )
             ORDER BY updated_at
             LIMIT 50
            """
        ).fetchall()

'''
if methods not in text:
    if anchor not in text:
        raise SystemExit("Ledger.due anchor not found")
    text = text.replace(anchor, methods + anchor, 1)

cycle_anchor = """    def cycle(self) -> None:
"""
reconcile = '''    def reconcile_handoffs(self) -> None:
        """Recover a child task if a restart/API failure interrupted a handoff."""
        for row in self.ledger.handoff_candidates():
            try:
                if row["task_type"] in {"BUILD", "REPAIR"}:
                    if not self.ledger.child(str(row["id"]), "REVIEW"):
                        self.enqueue_review(
                            row, int(row["pr_number"]), str(row["target_sha"])
                        )
                        self.runtime.event(
                            "HANDOFF_RECOVERED",
                            assignment_id=row["id"],
                            issue=row["issue_number"],
                            pr=row["pr_number"],
                            child_type="REVIEW",
                        )
                elif row["status"] == "DONE":
                    if not self.ledger.child(str(row["id"]), "REPAIR"):
                        blockers = json.loads(row["blockers_json"] or "[]")
                        self.enqueue_repair(row, blockers)
                        self.runtime.event(
                            "HANDOFF_RECOVERED",
                            assignment_id=row["id"],
                            issue=row["issue_number"],
                            pr=row["pr_number"],
                            child_type="REPAIR",
                        )
                elif row["status"] == "STALE" and row["pr_number"]:
                    pr = self.gh.pr(int(row["pr_number"]))
                    if str(pr.get("state") or "open").lower() != "open":
                        continue
                    current_sha = str(pr["head"]["sha"])
                    if current_sha == str(row["target_sha"]):
                        continue
                    if not self.ledger.child(str(row["id"]), "REVIEW", current_sha):
                        self.enqueue_replacement_review(row, current_sha)
                        self.runtime.event(
                            "HANDOFF_RECOVERED",
                            assignment_id=row["id"],
                            issue=row["issue_number"],
                            pr=row["pr_number"],
                            child_type="REVIEW",
                            target_sha=current_sha,
                        )
            except Exception as exc:
                self.runtime.event(
                    "HANDOFF_RECOVERY_RETRY",
                    assignment_id=row["id"],
                    issue=row["issue_number"],
                    pr=row["pr_number"],
                    error=str(exc),
                )

'''
if reconcile not in text:
    if cycle_anchor not in text:
        raise SystemExit("cycle anchor not found")
    text = text.replace(cycle_anchor, reconcile + cycle_anchor, 1)

old = """        self.sync_runtime_checkpoint()
        task = self.ledger.due()
"""
new = """        self.reconcile_handoffs()
        self.sync_runtime_checkpoint()
        task = self.ledger.due()
"""
if old not in text:
    raise SystemExit("cycle handoff insertion point not found")
text = text.replace(old, new, 1)

old = '''        unit = f"hl-ai-codex-{task['id'][:10]}-{int(time.time())}"
        self.runtime.run_started(run_id, task, prompt=prompt, systemd_unit=unit)
'''
new = '''        unit = f"hl-ai-codex-{task['id'][:10]}-{int(time.time())}"
        self.ledger.update(task["id"], systemd_unit=unit)
        self.runtime.run_started(run_id, task, prompt=prompt, systemd_unit=unit)
'''
if old not in text:
    raise SystemExit("Codex unit persistence point not found")
text = text.replace(old, new, 1)

old = '''                self.ledger.update(
                    task["id"],
                    status="WAITING_RATE_LIMIT",
                    retry_at=retry_at,
                    session_id=session_id,
                    last_error="Codex rate/usage limit",
                )
'''
new = '''                limit_text = bounded_limit_text(combined)
                self.ledger.update(
                    task["id"],
                    status="WAITING_RATE_LIMIT",
                    retry_at=retry_at,
                    session_id=session_id,
                    attempt=max(0, int(task["attempt"]) - 1),
                    limit_text=limit_text,
                    systemd_unit=None,
                    last_error="Codex rate/usage limit",
                )
                self.runtime.event(
                    "CODEX_WAITING_RATE_LIMIT",
                    assignment_id=task["id"],
                    issue=task["issue_number"],
                    pr=task["pr_number"],
                    session_id=session_id,
                    retry_after=retry_at,
                    limit_text=limit_text,
                )
'''
if old not in text:
    raise SystemExit("Codex rate-limit block not found")
text = text.replace(old, new, 1)

text = text.replace(
    '''                last_error="TEST_INJECTED_CODEX_SESSION_END",
            )
''',
    '''                last_error="TEST_INJECTED_CODEX_SESSION_END",
                systemd_unit=None,
            )
''',
    1,
)
text = text.replace(
    '''                retry_at=None,
                last_error=None,
            )
            self.enqueue_review(task, pr_number, new_sha)
''',
    '''                retry_at=None,
                last_error=None,
                systemd_unit=None,
            )
            self.enqueue_review(task, pr_number, new_sha)
''',
    1,
)
text = text.replace(
    '''            self.ledger.update(task["id"], status="STALE", last_error="PR changed during review")
''',
    '''            self.ledger.update(
                task["id"], status="STALE", last_error="PR changed during review",
                systemd_unit=None,
            )
''',
    1,
)
text = text.replace(
    '''                session_id=session_id,
                last_error="review FAIL",
            )
''',
    '''                session_id=session_id,
                last_error="review FAIL",
                systemd_unit=None,
            )
''',
    1,
)
text = text.replace(
    '''                retry_at=utcnow(),
                last_error=None,
            )
''',
    '''                retry_at=utcnow(),
                last_error=None,
                systemd_unit=None,
            )
''',
    1,
)

old = '''        self.ledger.update(task["id"], status="BLOCKED", retry_at=None, last_error=error[:1500])
'''
new = '''        self.ledger.update(
            task["id"], status="BLOCKED", retry_at=None,
            last_error=error[:1500], systemd_unit=None,
        )
'''
if old not in text:
    raise SystemExit("block() update not found")
text = text.replace(old, new, 1)

old = '''    def enqueue_review(self, parent: sqlite3.Row, pr_number: int, sha: str) -> None:
        issue = self.gh.issue(int(parent["issue_number"]))
'''
new = '''    def enqueue_review(self, parent: sqlite3.Row, pr_number: int, sha: str) -> None:
        if self.ledger.child(str(parent["id"]), "REVIEW"):
            return
        issue = self.gh.issue(int(parent["issue_number"]))
'''
if old not in text:
    raise SystemExit("enqueue_review signature not found")
text = text.replace(old, new, 1)

old = '''        self.gh.comment(
            pr_number,
            assignment_marker(
                task_id=task_id,
                agent="CLAUDE",
                task_type="REVIEW",
                model_class=model,
                task_class=str(parent["task_class"]),
                issue_number=int(parent["issue_number"]),
                pr_number=pr_number,
                target_sha=sha,
                parent_id=str(parent["id"]),
                previous_sha=parent["previous_sha"],
                escalation_reason=escalation_reason,
            ),
        )
        self.gh.add_labels(pr_number, [self.cfg["labels"]["waiting_review"]])
'''
new = '''        try:
            self.gh.comment(
                pr_number,
                assignment_marker(
                    task_id=task_id,
                    agent="CLAUDE",
                    task_type="REVIEW",
                    model_class=model,
                    task_class=str(parent["task_class"]),
                    issue_number=int(parent["issue_number"]),
                    pr_number=pr_number,
                    target_sha=sha,
                    parent_id=str(parent["id"]),
                    previous_sha=parent["previous_sha"],
                    escalation_reason=escalation_reason,
                ),
            )
            self.gh.add_labels(pr_number, [self.cfg["labels"]["waiting_review"]])
        except Exception as exc:
            self.runtime.event(
                "HANDOFF_MIRROR_FAILED", assignment_id=task_id,
                issue=parent["issue_number"], pr=pr_number,
                child_type="REVIEW", error=str(exc),
            )
'''
if old not in text:
    raise SystemExit("enqueue_review mirror block not found")
text = text.replace(old, new, 1)

old = '''    def enqueue_repair(self, review: sqlite3.Row, blockers: list[str]) -> None:
        task_id = self.ledger.create_task(
'''
new = '''    def enqueue_repair(self, review: sqlite3.Row, blockers: list[str]) -> None:
        if self.ledger.child(str(review["id"]), "REPAIR"):
            return
        task_id = self.ledger.create_task(
'''
if old not in text:
    raise SystemExit("enqueue_repair signature not found")
text = text.replace(old, new, 1)

old = '''        self.gh.comment(
            int(review["pr_number"]),
            assignment_marker(
                task_id=task_id,
                agent="CODEX_CHATGPT",
                task_type="REPAIR",
                model_class="CODEX_DEFAULT",
                task_class=str(review["task_class"]),
                issue_number=int(review["issue_number"]),
                pr_number=int(review["pr_number"]),
                target_sha=str(review["target_sha"]),
                parent_id=str(review["id"]),
                previous_sha=str(review["target_sha"]),
            ),
        )
'''
new = '''        try:
            self.gh.comment(
                int(review["pr_number"]),
                assignment_marker(
                    task_id=task_id,
                    agent="CODEX_CHATGPT",
                    task_type="REPAIR",
                    model_class="CODEX_DEFAULT",
                    task_class=str(review["task_class"]),
                    issue_number=int(review["issue_number"]),
                    pr_number=int(review["pr_number"]),
                    target_sha=str(review["target_sha"]),
                    parent_id=str(review["id"]),
                    previous_sha=str(review["target_sha"]),
                ),
            )
        except Exception as exc:
            self.runtime.event(
                "HANDOFF_MIRROR_FAILED", assignment_id=task_id,
                issue=review["issue_number"], pr=review["pr_number"],
                child_type="REPAIR", error=str(exc),
            )
'''
if old not in text:
    raise SystemExit("enqueue_repair mirror block not found")
text = text.replace(old, new, 1)

old = '''    def enqueue_replacement_review(self, old: sqlite3.Row, current_sha: str) -> None:
        task_id = self.ledger.create_task(
'''
new = '''    def enqueue_replacement_review(self, old: sqlite3.Row, current_sha: str) -> None:
        if self.ledger.child(str(old["id"]), "REVIEW", current_sha):
            return
        task_id = self.ledger.create_task(
'''
if old not in text:
    raise SystemExit("enqueue_replacement_review signature not found")
text = text.replace(old, new, 1)

old = '''        self.gh.comment(
            int(old["pr_number"]),
            assignment_marker(
                task_id=task_id,
                agent="CLAUDE",
                task_type="REVIEW",
                model_class=str(old["model_class"]),
                task_class=str(old["task_class"]),
                issue_number=int(old["issue_number"]),
                pr_number=int(old["pr_number"]),
                target_sha=current_sha,
                parent_id=str(old["id"]),
                previous_sha=str(old["target_sha"]),
            ),
        )
'''
new = '''        try:
            self.gh.comment(
                int(old["pr_number"]),
                assignment_marker(
                    task_id=task_id,
                    agent="CLAUDE",
                    task_type="REVIEW",
                    model_class=str(old["model_class"]),
                    task_class=str(old["task_class"]),
                    issue_number=int(old["issue_number"]),
                    pr_number=int(old["pr_number"]),
                    target_sha=current_sha,
                    parent_id=str(old["id"]),
                    previous_sha=str(old["target_sha"]),
                ),
            )
        except Exception as exc:
            self.runtime.event(
                "HANDOFF_MIRROR_FAILED", assignment_id=task_id,
                issue=old["issue_number"], pr=old["pr_number"],
                child_type="REVIEW", target_sha=current_sha, error=str(exc),
            )
'''
if old not in text:
    raise SystemExit("replacement review mirror block not found")
text = text.replace(old, new, 1)

p.write_text(text, encoding="utf-8")

tests = Path("tests/test_ai_team_orchestrator.py")
test_text = tests.read_text(encoding="utf-8")
if "def test_handoffs_are_idempotent_and_recoverable()" not in test_text:
    test_text += '''


def test_handoffs_are_idempotent_and_recoverable():
    source = MODULE_PATH.read_text()
    assert "def reconcile_handoffs" in source
    assert "def handoff_candidates" in source
    assert "def child(" in source
    assert "HANDOFF_RECOVERED" in source
    assert "HANDOFF_RECOVERY_RETRY" in source
    assert "HANDOFF_MIRROR_FAILED" in source
    assert 'self.ledger.child(str(parent["id"]), "REVIEW")' in source
    assert 'self.ledger.child(str(review["id"]), "REPAIR")' in source
    assert 'self.ledger.child(str(old["id"]), "REVIEW", current_sha)' in source


def test_codex_limit_and_worker_state_do_not_consume_or_leak():
    source = MODULE_PATH.read_text()
    codex = source[source.index("    def handle_codex("):source.index("    def invoke_codex(")]
    assert "CODEX_WAITING_RATE_LIMIT" in codex
    assert 'attempt=max(0, int(task["attempt"]) - 1)' in codex
    assert 'self.ledger.update(task["id"], systemd_unit=unit)' in codex
    assert "limit_text=limit_text" in codex
    assert "systemd_unit=None" in codex


def test_terminal_block_releases_worker_marker():
    source = MODULE_PATH.read_text()
    block = source[source.index("    def block("):source.index("    def sync_ready(")]
    assert 'status="BLOCKED"' in block
    assert "systemd_unit=None" in block
'''
    tests.write_text(test_text, encoding="utf-8")
