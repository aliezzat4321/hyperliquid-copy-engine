#!/usr/bin/env python3
from pathlib import Path

path = Path("scripts/ai_team_orchestrator.py")
text = path.read_text()


def replace_once(old: str, new: str, label: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one anchor, found {count}")
    text = text.replace(old, new, 1)


replace_once(
    'from typing import Any\n\nREPO = "aliezzat4321/hyperliquid-copy-engine"\n',
    'from typing import Any\n\n'
    'sys.path.insert(0, str(Path(__file__).resolve().parent))\n'
    'from ai_team_runtime_ledger import RuntimeLedgerFiles, bounded_redacted\n\n'
    'REPO = "aliezzat4321/hyperliquid-copy-engine"\n',
    "runtime import",
)

replace_once(
    'CLAUDE_ENV_FILE = Path("/etc/hyperliquid-ai-team/claude.env")\n'
    'GIT_PUSH_REMOTE = f"git@github.com:{REPO}.git"\n',
    'CLAUDE_ENV_FILE = Path("/etc/hyperliquid-ai-team/claude.env")\n'
    'CLAUDE_CREDENTIALS = CLAUDE_HOME / ".claude" / ".credentials.json"\n'
    'RUNTIME_STATUS_ISSUE = 130\n'
    'GIT_PUSH_REMOTE = f"git@github.com:{REPO}.git"\n',
    "runtime constants",
)

old_preflight = '''def codex_runtime_preflight(
    codex_path: Path = Path("/usr/local/bin/codex"),
) -> Path:
    """Refuse a model call if Codex or its required Code Mode host is missing."""
    if not codex_path.is_file() or not os.access(codex_path, os.X_OK):
        raise RuntimeError(f"Codex CLI missing or not executable: {codex_path}")
    host = codex_path.with_name("codex-code-mode-host")
    if not host.is_file() or not os.access(host, os.X_OK):
        raise RuntimeError(
            "Codex Code Mode host missing or not executable; refusing model call: "
            f"{host}"
        )
    return host


'''
new_preflight = '''def codex_runtime_preflight(
    codex_path: Path = Path("/usr/local/bin/codex"),
) -> Path:
    """Refuse a model call if Codex or its Linux sandbox dependencies are missing."""
    if not codex_path.is_file() or not os.access(codex_path, os.X_OK):
        raise RuntimeError(f"Codex CLI missing or not executable: {codex_path}")
    host = codex_path.with_name("codex-code-mode-host")
    if not host.is_file() or not os.access(host, os.X_OK):
        raise RuntimeError(
            "Codex Code Mode host missing or not executable; refusing model call: "
            f"{host}"
        )
    bwrap = Path("/usr/bin/bwrap")
    if not bwrap.is_file() or not os.access(bwrap, os.X_OK):
        raise RuntimeError("Codex Linux workspace sandbox dependency missing: /usr/bin/bwrap")
    return host


def claude_runtime_preflight(
    claude_path: Path = Path("/usr/bin/claude"),
    credentials: Path = CLAUDE_CREDENTIALS,
) -> Path:
    """Use the isolated Claude subscription OAuth credential, never a copied setup-token."""
    if not claude_path.is_file() or not os.access(claude_path, os.X_OK):
        raise RuntimeError(f"Claude CLI missing or not executable: {claude_path}")
    if not credentials.is_file():
        raise RuntimeError(f"Claude subscription credential missing: {credentials}")
    return credentials


def acceptance_flag(body: str, name: str) -> bool:
    return bool(re.search(rf"(?mi)^\\s*{re.escape(name)}\\s*=\\s*YES\\s*$", body))


def retry_at_after(seconds: int) -> str:
    value = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


'''
replace_once(old_preflight, new_preflight, "preflight helpers")

old_init = '''class Orchestrator:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.gh = GitHub(REPO)
        self.ledger = Ledger(DB_PATH)
        self.trusted = set(self.cfg["trusted_author_associations"])

    def claim_ready_issue(self) -> bool:
'''
new_init = '''class Orchestrator:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.gh = GitHub(REPO)
        self.ledger = Ledger(DB_PATH)
        self.runtime = RuntimeLedgerFiles(STATE_ROOT, DB_PATH, REPO, RUNTIME_STATUS_ISSUE)
        self.trusted = set(self.cfg["trusted_author_associations"])

    def sync_runtime_checkpoint(self) -> None:
        """Project SQLite runtime state and mirror a compact chat-independent handoff."""
        try:
            main = self.gh.api("GET", f"repos/{REPO}/commits/main") or {}
            rows = self.gh.api("GET", f"repos/{REPO}/issues?state=open&per_page=100") or []
            priorities = []
            for row in rows:
                if "pull_request" in row:
                    continue
                title = str(row.get("title") or "")
                if not title.upper().startswith(("P0", "P1")):
                    continue
                priorities.append({"issue": int(row["number"]), "title": title[:160]})
            snap = self.ledger.status_snapshot()
            cur = snap.get("current") or {}
            pending_owner_action = None
            if "AUTH_REQUIRED" in str(cur.get("last_error") or ""):
                pending_owner_action = str(cur.get("last_error"))[:500]
            body = self.runtime.handoff(
                main_head=str(main.get("sha") or "UNKNOWN"),
                active_priorities=priorities,
                pending_owner_action=pending_owner_action,
            )
            status_issue = self.gh.issue(RUNTIME_STATUS_ISSUE)
            if str(status_issue.get("body") or "") != body:
                self.gh.api(
                    "PATCH",
                    f"repos/{REPO}/issues/{RUNTIME_STATUS_ISSUE}",
                    {"body": body},
                )
        except Exception as exc:
            self.runtime.event("CHECKPOINT_MIRROR_FAILED", error=str(exc))

    def finish_runtime_run(
        self,
        run_id: int,
        task_id: str,
        *,
        stdout: str | None,
        stderr: str | None,
        exit_code: int,
        session_id: str | None,
        usage: dict[str, int],
        result: str | None,
        error: str | None = None,
        status: str | None = None,
        blockers: list[str] | None = None,
    ) -> None:
        final = self.ledger.get(task_id)
        final_error = error if error is not None else final["last_error"]
        self.runtime.run_finished(
            run_id,
            final,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            session_id=session_id,
            usage=usage,
            result=result,
            error=final_error,
            status=status or str(final["status"]),
            retry_after=final["retry_at"],
            blockers=blockers,
        )
        self.sync_runtime_checkpoint()

    def claim_ready_issue(self) -> bool:
'''
replace_once(old_init, new_init, "orchestrator runtime helpers")

replace_once(
    '''            self.gh.add_labels(number, [self.cfg["labels"]["pending"]])
            self.gh.remove_label(number, label)
            return True
''',
    '''            self.gh.add_labels(number, [self.cfg["labels"]["pending"]])
            self.gh.remove_label(number, label)
            self.runtime.event(
                "TASK_ASSIGNED",
                assignment_id=task_id,
                issue=number,
                agent="CODEX_CHATGPT",
                task_type="BUILD",
            )
            self.sync_runtime_checkpoint()
            return True
''',
    "assignment event",
)

old_cycle = '''    def cycle(self) -> None:
        self.ledger.recover_interrupted()
        task = self.ledger.due()
        if task is None:
            self.claim_ready_issue()
            task = self.ledger.due()
        if task is None:
            return
        if task["status"] == "WAITING_CI":
            self.handle_ci(task)
        elif task["task_type"] in {"BUILD", "REPAIR"}:
            self.handle_codex(task)
        elif task["task_type"] == "REVIEW":
            self.handle_review(task)
        else:
            self.block(task, f"unsupported task type {task['task_type']}")
'''
new_cycle = '''    def cycle(self) -> None:
        self.ledger.recover_interrupted()
        self.sync_runtime_checkpoint()
        task = self.ledger.due()
        if task is None:
            self.claim_ready_issue()
            task = self.ledger.due()
        if task is None:
            return
        try:
            if task["status"] == "WAITING_CI":
                self.handle_ci(task)
            elif task["task_type"] in {"BUILD", "REPAIR"}:
                self.handle_codex(task)
            elif task["task_type"] == "REVIEW":
                self.handle_review(task)
            else:
                self.block(task, f"unsupported task type {task['task_type']}")
        finally:
            self.sync_runtime_checkpoint()
'''
replace_once(old_cycle, new_cycle, "cycle checkpoint")

replace_once(
    '''        run_id = self.ledger.open_run(task, log_path)
        try:
            cp = self.invoke_codex(task, workdir, prompt)
''',
    '''        run_id = self.ledger.open_run(task, log_path)
        unit = f"hl-ai-codex-{task['id'][:10]}-{int(time.time())}"
        self.runtime.run_started(run_id, task, prompt=prompt, systemd_unit=unit)
        self.sync_runtime_checkpoint()
        try:
            cp = self.invoke_codex(task, workdir, prompt, unit)
''',
    "Codex run start",
)

replace_once(
    '''            log_path.write_text(text)
            self.ledger.close_run(
                run_id,
                exit_code=124,
                session_id=session_id,
                usage=usage,
                result=result,
                error="Codex timeout",
            )
            self.retry_or_block(task, "Codex timeout", session_id=session_id, rate_limited=False)
            return
''',
    '''            log_path.write_text(bounded_redacted(text))
            self.ledger.close_run(
                run_id,
                exit_code=124,
                session_id=session_id,
                usage=usage,
                result=result,
                error="Codex timeout",
            )
            self.retry_or_block(task, "Codex timeout", session_id=session_id, rate_limited=False)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                exit_code=124,
                session_id=session_id,
                usage=usage,
                result=result,
                error="Codex timeout",
            )
            return
''',
    "Codex timeout checkpoint",
)

replace_once(
    '''        log_path.write_text(combined)
        session_id, usage, result = parse_codex_stream(cp.stdout)
''',
    '''        log_path.write_text(bounded_redacted(combined))
        session_id, usage, result = parse_codex_stream(cp.stdout)
''',
    "Codex log redaction",
)

replace_once(
    '''                self.ledger.update(
                    task["id"],
                    status="WAITING_RATE_LIMIT",
                    retry_at=retry_at,
                    session_id=session_id,
                    last_error="Codex rate/usage limit",
                )
                return
            self.retry_or_block(task, f"Codex failed rc={cp.returncode}", session_id=session_id)
            return
        try:
''',
    '''                self.ledger.update(
                    task["id"],
                    status="WAITING_RATE_LIMIT",
                    retry_at=retry_at,
                    session_id=session_id,
                    last_error="Codex rate/usage limit",
                )
                self.finish_runtime_run(
                    run_id,
                    str(task["id"]),
                    stdout=cp.stdout,
                    stderr=cp.stderr,
                    exit_code=cp.returncode,
                    session_id=session_id,
                    usage=usage,
                    result=result,
                    error="Codex rate/usage limit",
                )
                return
            self.retry_or_block(task, f"Codex failed rc={cp.returncode}", session_id=session_id)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=cp.returncode,
                session_id=session_id,
                usage=usage,
                result=result,
            )
            return
        if (
            acceptance_flag(str(issue.get("body") or ""), "AI_TEAM_TEST_CODEX_INTERRUPT_ONCE")
            and int(task["attempt"]) == 1
        ):
            retry_at = retry_at_after(300)
            self.ledger.update(
                task["id"],
                status="RETRY",
                retry_at=retry_at,
                session_id=session_id,
                last_error="TEST_INJECTED_CODEX_SESSION_END",
            )
            self.runtime.event(
                "TEST_CODEX_SESSION_INTERRUPTED",
                assignment_id=task["id"],
                issue=task["issue_number"],
                session_id=session_id,
                retry_after=retry_at,
            )
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=0,
                session_id=session_id,
                usage=usage,
                result=result,
                error="TEST_INJECTED_CODEX_SESSION_END",
                status="RETRY",
            )
            return
        try:
''',
    "Codex failure/interrupt recovery",
)

replace_once(
    '''            self.enqueue_review(task, pr_number, new_sha)
        except Exception as exc:
            self.block(self.ledger.get(task["id"]), str(exc))

    def invoke_codex(
        self, task: sqlite3.Row, workdir: Path, prompt: str
    ) -> subprocess.CompletedProcess[str]:
''',
    '''            self.enqueue_review(task, pr_number, new_sha)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=0,
                session_id=session_id,
                usage=usage,
                result=result,
                status="DONE",
            )
        except Exception as exc:
            self.block(self.ledger.get(task["id"]), str(exc))
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=1,
                session_id=session_id,
                usage=usage,
                result=result,
                error=str(exc),
                status="BLOCKED",
            )

    def invoke_codex(
        self, task: sqlite3.Row, workdir: Path, prompt: str, unit: str
    ) -> subprocess.CompletedProcess[str]:
''',
    "Codex success/blocked checkpoint",
)

replace_once(
    '''        unit = f"hl-ai-codex-{task['id'][:10]}-{int(time.time())}"
        full = model_sandbox_command(
''',
    '''        full = model_sandbox_command(
''',
    "Codex unit ownership",
)

# Review always uses the isolated subscription credential; the old setup-token
# EnvironmentFile path is intentionally removed.
replace_once(
    '''        model = str(task["model_class"])
        if model == "OPUS":
            issue = self.gh.issue(int(task["issue_number"]))
            _, reason = parse_task_class(str(issue.get("body") or ""))
            route_review(self.cfg, str(task["task_class"]), reason)
''',
    '''        model = str(task["model_class"])
        issue = self.gh.issue(int(task["issue_number"]))
        if model == "OPUS":
            _, reason = parse_task_class(str(issue.get("body") or ""))
            route_review(self.cfg, str(task["task_class"]), reason)
        try:
            claude_runtime_preflight()
        except RuntimeError as exc:
            self.block(task, f"CLAUDE_AUTH_REQUIRED: {exc}")
            return
''',
    "Claude subscription preflight",
)

old_auth_block = '''        run_id = self.ledger.open_run(task, log_path)
        if not CLAUDE_ENV_FILE.exists():
            self.ledger.close_run(
                run_id,
                exit_code=78,
                session_id=None,
                usage={},
                result=None,
                error=f"Claude auth file missing: {CLAUDE_ENV_FILE}",
            )
            self.ledger.update(
                task["id"],
                status="BLOCKED",
                last_error="CLAUDE_AUTH_REQUIRED: run owner setup-token helper",
            )
            return
        try:
            cp = self.invoke_claude(task, workdir, prompt)
'''
new_auth_block = '''        run_id = self.ledger.open_run(task, log_path)
        unit = f"hl-ai-claude-{task['id'][:10]}-{int(time.time())}"
        self.runtime.run_started(run_id, task, prompt=prompt, systemd_unit=unit)
        self.sync_runtime_checkpoint()
        try:
            cp = self.invoke_claude(task, workdir, prompt, unit)
'''
replace_once(old_auth_block, new_auth_block, "Claude run start")

replace_once(
    '''            log_path.write_text(text)
            self.ledger.close_run(
                run_id,
                exit_code=124,
                session_id=session_id,
                usage=usage,
                result=result,
                error="Claude timeout",
            )
            self.retry_or_block(task, "Claude timeout", session_id=session_id)
            return
''',
    '''            log_path.write_text(bounded_redacted(text))
            self.ledger.close_run(
                run_id,
                exit_code=124,
                session_id=session_id,
                usage=usage,
                result=result,
                error="Claude timeout",
            )
            self.retry_or_block(task, "Claude timeout", session_id=session_id)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                exit_code=124,
                session_id=session_id,
                usage=usage,
                result=result,
                error="Claude timeout",
            )
            return
''',
    "Claude timeout checkpoint",
)

replace_once(
    '''        log_path.write_text(combined)
        session_id, usage, result = parse_claude_output(cp.stdout)
''',
    '''        log_path.write_text(bounded_redacted(combined))
        session_id, usage, result = parse_claude_output(cp.stdout)
''',
    "Claude log redaction",
)

replace_once(
    '''                self.ledger.update(
                    task["id"],
                    status="WAITING_RATE_LIMIT",
                    retry_at=retry_at,
                    session_id=session_id,
                    last_error="Claude rate/usage limit",
                )
                return
            self.retry_or_block(task, f"Claude failed rc={cp.returncode}", session_id=session_id)
            return
        try:
            verdict, blockers, summary = extract_review(result, target_sha)
''',
    '''                self.ledger.update(
                    task["id"],
                    status="WAITING_RATE_LIMIT",
                    retry_at=retry_at,
                    session_id=session_id,
                    last_error="Claude rate/usage limit",
                )
                self.finish_runtime_run(
                    run_id,
                    str(task["id"]),
                    stdout=cp.stdout,
                    stderr=cp.stderr,
                    exit_code=cp.returncode,
                    session_id=session_id,
                    usage=usage,
                    result=result,
                    error="Claude rate/usage limit",
                )
                return
            self.retry_or_block(task, f"Claude failed rc={cp.returncode}", session_id=session_id)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=cp.returncode,
                session_id=session_id,
                usage=usage,
                result=result,
            )
            return
        if (
            acceptance_flag(str(issue.get("body") or ""), "AI_TEAM_TEST_CLAUDE_RATE_LIMIT_ONCE")
            and int(task["attempt"]) == 1
        ):
            retry_at = retry_at_after(300)
            self.ledger.update(
                task["id"],
                status="WAITING_RATE_LIMIT",
                retry_at=retry_at,
                session_id=session_id,
                last_error="TEST_INJECTED_CLAUDE_RATE_LIMIT",
            )
            self.runtime.event(
                "TEST_CLAUDE_RATE_LIMIT_INJECTED",
                assignment_id=task["id"],
                issue=task["issue_number"],
                pr=task["pr_number"],
                target_sha=target_sha,
                session_id=session_id,
                retry_after=retry_at,
            )
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=75,
                session_id=session_id,
                usage=usage,
                result=result,
                error="TEST_INJECTED_CLAUDE_RATE_LIMIT",
                status="WAITING_RATE_LIMIT",
            )
            return
        try:
            verdict, blockers, summary = extract_review(result, target_sha)
''',
    "Claude failure/rate recovery",
)

replace_once(
    '''        except Exception as exc:
            self.retry_or_block(task, str(exc), session_id=session_id)
            return
        after = self.gh.pr(int(task["pr_number"]))
''',
    '''        except Exception as exc:
            self.retry_or_block(task, str(exc), session_id=session_id)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=1,
                session_id=session_id,
                usage=usage,
                result=result,
                error=str(exc),
            )
            return
        after = self.gh.pr(int(task["pr_number"]))
''',
    "Claude parse checkpoint",
)

replace_once(
    '''            self.ledger.update(task["id"], status="STALE", last_error="PR changed during review")
            self.enqueue_replacement_review(task, str(after["head"]["sha"]))
            return
''',
    '''            self.ledger.update(task["id"], status="STALE", last_error="PR changed during review")
            self.enqueue_replacement_review(task, str(after["head"]["sha"]))
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=1,
                session_id=session_id,
                usage=usage,
                result=result,
                error="PR changed during review",
                status="STALE",
            )
            return
''',
    "Claude stale checkpoint",
)

replace_once(
    '''            self.enqueue_repair(task, blockers)
        else:
            self.ledger.update(
                task["id"],
                status="WAITING_CI",
                blockers_json="[]",
                session_id=session_id,
                retry_at=utcnow(),
                last_error=None,
            )

    def invoke_claude(
        self, task: sqlite3.Row, workdir: Path, prompt: str
    ) -> subprocess.CompletedProcess[str]:
''',
    '''            self.enqueue_repair(task, blockers)
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=0,
                session_id=session_id,
                usage=usage,
                result=result,
                status="FAIL",
                blockers=blockers,
            )
        else:
            self.ledger.update(
                task["id"],
                status="WAITING_CI",
                blockers_json="[]",
                session_id=session_id,
                retry_at=utcnow(),
                last_error=None,
            )
            self.finish_runtime_run(
                run_id,
                str(task["id"]),
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=0,
                session_id=session_id,
                usage=usage,
                result=result,
                status="PASS",
                blockers=[],
            )

    def invoke_claude(
        self, task: sqlite3.Row, workdir: Path, prompt: str, unit: str
    ) -> subprocess.CompletedProcess[str]:
''',
    "Claude verdict checkpoint",
)

replace_once(
    '''        unit = f"hl-ai-claude-{task['id'][:10]}-{int(time.time())}"
        full = model_sandbox_command(
            unit=unit,
            user=CLAUDE_USER,
            home=CLAUDE_HOME,
            workdir=workdir,
            command=command,
            env_file=CLAUDE_ENV_FILE,
        )
''',
    '''        full = model_sandbox_command(
            unit=unit,
            user=CLAUDE_USER,
            home=CLAUDE_HOME,
            workdir=workdir,
            command=command,
        )
''',
    "Claude subscription invocation",
)

replace_once(
    '''        except Exception:
            pass


def print_status(ledger: Ledger) -> None:
''',
    '''        except Exception:
            pass
        try:
            self.runtime.event(
                "TASK_BLOCKED",
                assignment_id=task["id"],
                issue=task["issue_number"],
                pr=task["pr_number"],
                agent=task["agent"],
                error=error,
            )
            self.sync_runtime_checkpoint()
        except Exception:
            pass


def print_status(ledger: Ledger) -> None:
''',
    "blocked event",
)

replace_once(
    '''    print(f"last_successful_run={snap['last_success'] or 'NONE'}")
    print("recent_failures=" + json.dumps(snap["failures"], separators=(",", ":")))
''',
    '''    print(f"last_successful_run={snap['last_success'] or 'NONE'}")
    print("recent_failures=" + json.dumps(snap["failures"], separators=(",", ":")))
    runtime = RuntimeLedgerFiles(STATE_ROOT, DB_PATH, REPO, RUNTIME_STATUS_ISSUE)
    projection = runtime.project_current()
    print(f"runtime_status_issue={RUNTIME_STATUS_ISSUE}")
    for agent_key in ("codex", "claude"):
        assignment = projection.get("assignment", {}).get(agent_key)
        if assignment:
            print(f"{agent_key}_current_step={assignment.get('current_step') or 'NONE'}")
            print(f"{agent_key}_next_step={assignment.get('next_step') or 'NONE'}")
            print(f"{agent_key}_checkpoint_retry={assignment.get('retry_after') or 'NONE'}")
        else:
            print(f"{agent_key}_current_step=IDLE")
            print(f"{agent_key}_next_step=NONE")
            print(f"{agent_key}_checkpoint_retry=NONE")
''',
    "status projection",
)

path.write_text(text)
print("ISSUE129_ORCHESTRATOR_PATCH=APPLIED")
