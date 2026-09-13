#!/usr/bin/env python3
"""One-shot, fail-closed Opus review for the exact P0 #90 storage plan.

This runner never mutates storage. It only creates a root-owned ledger task/run,
invokes Claude Opus as the dedicated non-root Claude user with /root and /mnt
hidden in a private mount namespace, validates the exact approval markers, and
runs the existing trusted verifier.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = "aliezzat4321/hyperliquid-copy-engine"
ISSUE = 90
PR = 288
CODE_SHA = "d763b536edd9d6fc5fce76105996bfaf5858d85b"
PLAN_RUN_ID = "34766743062"
ARTIFACT_NAME = f"p0-90-storage-review-{CODE_SHA}"
RUNTIME_SCRIPTS = Path("/opt/hyperliquid-ai-team/scripts")
DB = Path("/var/lib/hyperliquid-ai-team/orchestrator/ledger.sqlite3")
RUNTIME_ROOT = Path("/var/lib/hyperliquid-ai-team")

sys.path.insert(0, str(RUNTIME_SCRIPTS))
import ai_team_orchestrator as orch  # noqa: E402


def api(path: str) -> dict:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("missing GitHub token")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stop_unit(name: str) -> None:
    subprocess.run(["systemctl", "stop", name], check=False, capture_output=True, text=True)


def start_unit(name: str) -> None:
    subprocess.run(["systemctl", "start", name], check=False, capture_output=True, text=True)


def main() -> int:
    bundle = Path(os.environ["BUNDLE_DIR"]).resolve()
    required_paths = {
        "RETENTION_MANIFEST_SHA256": bundle / "retention" / "storage_retention_manifest.json",
        "BOOTSTRAP_PLAN_SHA256": bundle / "bootstrap-plan.json",
        "LIFECYCLE_MANIFEST_SHA256": bundle / "lifecycle-plan.json",
        "REVIEW_BINDING_SHA256": bundle / "review-binding.json",
    }
    for path in required_paths.values():
        if not path.is_file():
            raise RuntimeError(f"missing immutable bundle file: {path}")

    pr = api(f"pulls/{PR}")
    if not pr.get("merged_at") or pr["head"]["sha"] != CODE_SHA:
        raise RuntimeError("PR #288 is not merged at the exact reviewed head")
    run = api(f"actions/runs/{PLAN_RUN_ID}")
    if run.get("conclusion") != "success" or run.get("head_sha") != CODE_SHA:
        raise RuntimeError("fresh plan run is not successful at exact reviewed code")
    if run.get("name") != "p0-90-storage-recovery-plan":
        raise RuntimeError("unexpected plan workflow")
    completed = run.get("updated_at") or run.get("created_at")
    if not completed:
        raise RuntimeError("plan run has no completion timestamp")
    age = (
        datetime.now(timezone.utc)
        - datetime.fromisoformat(completed.replace("Z", "+00:00"))
    ).total_seconds()
    if age < 0 or age > 3600:
        raise RuntimeError(f"fresh plan expired: age_seconds={age:.0f}")

    hashes = {name: sha256(path) for name, path in required_paths.items()}
    binding = json.loads(required_paths["REVIEW_BINDING_SHA256"].read_text())
    binding_required = {
        "code_sha": CODE_SHA,
        "retention_manifest_sha256": hashes["RETENTION_MANIFEST_SHA256"],
        "bootstrap_plan_sha256": hashes["BOOTSTRAP_PLAN_SHA256"],
        "lifecycle_manifest_sha256": hashes["LIFECYCLE_MANIFEST_SHA256"],
        "postgresql_filesystem_deletion": False,
        "polymarket_mutation": False,
        "real_trading_changed": False,
        "mutation_performed": False,
    }
    for key, value in binding_required.items():
        if binding.get(key) != value:
            raise RuntimeError(f"review binding mismatch: {key}")

    o = orch.Orchestrator()
    ledger = o.ledger
    task_id = uuid.uuid4().hex[:16]
    workdir = orch.prepare_checkout(
        user=orch.CLAUDE_USER,
        home=orch.CLAUDE_HOME,
        base_dir=orch.CLAUDE_WORK,
        task_id=task_id,
        ref=CODE_SHA,
        branch=None,
    )
    review_bundle = workdir / "review_bundle"
    if review_bundle.exists():
        shutil.rmtree(review_bundle)
    shutil.copytree(bundle, review_bundle)
    uid, gid = orch.normalize_worktree_ownership(workdir, orch.CLAUDE_USER)

    ledger.create_task(
        id=task_id,
        issue_number=ISSUE,
        pr_number=PR,
        task_type="DESTRUCTIVE_REVIEW",
        agent="CLAUDE",
        model_class="OPUS",
        task_class="ROUTINE",
        queue_priority=-100000,
        status="RUNNING",
        target_sha=CODE_SHA,
        workdir=str(workdir),
        lifecycle_phase="DESTRUCTIVE_REVIEW",
        completion_contract={"version": 1, "close_on_merge": False, "requirements": ["STORAGE_PROOF"]},
        evidence={
            "requirement": "STORAGE_PROOF",
            "trusted_plan": {
                "plan_run_id": PLAN_RUN_ID,
                "plan_completed_at": completed,
                "retention_sha": hashes["RETENTION_MANIFEST_SHA256"],
                "bootstrap_sha": hashes["BOOTSTRAP_PLAN_SHA256"],
                "lifecycle_sha": hashes["LIFECYCLE_MANIFEST_SHA256"],
                "binding_sha": hashes["REVIEW_BINDING_SHA256"],
            },
        },
    )
    task = ledger.get(task_id)
    log_path = orch.CLAUDE_LOG / f"{task_id}-direct.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    run_id = ledger.open_run(task, log_path)

    prompt = f"""You are Claude Opus, the independent destructive-storage reviewer for Hyperliquid issue #90.
Review only the exact immutable storage bundle in review_bundle. Do not modify files and do not perform deletion.

Trusted binding:
ASSIGNMENT_ID={task_id}
TARGET_SHA={CODE_SHA}
PLAN_RUN_ID={PLAN_RUN_ID}
PLAN_COMPLETED_AT={completed}
RETENTION_MANIFEST_SHA256={hashes['RETENTION_MANIFEST_SHA256']}
BOOTSTRAP_PLAN_SHA256={hashes['BOOTSTRAP_PLAN_SHA256']}
LIFECYCLE_MANIFEST_SHA256={hashes['LIFECYCLE_MANIFEST_SHA256']}
REVIEW_BINDING_SHA256={hashes['REVIEW_BINDING_SHA256']}
PR_NUMBER=288

Verify with targeted local checks that deletion is bounded to Hyperliquid market-shadow candidates, lifecycle mutation is COMPRESS_CANDIDATE-only, protected/recent/robust/profitability data is excluded, PostgreSQL filesystem deletion is forbidden, Polymarket mutation is forbidden, and real trading remains off.

If and only if the exact plan is safe to apply, end with EXACTLY these lines:
SECOND_PASS_GATE=PASS
MODEL_CLASS=OPUS
ASSIGNMENT_ID={task_id}
TARGET_SHA={CODE_SHA}
PLAN_RUN_ID={PLAN_RUN_ID}
RETENTION_MANIFEST_SHA256={hashes['RETENTION_MANIFEST_SHA256']}
BOOTSTRAP_PLAN_SHA256={hashes['BOOTSTRAP_PLAN_SHA256']}
LIFECYCLE_MANIFEST_SHA256={hashes['LIFECYCLE_MANIFEST_SHA256']}
REVIEW_BINDING_SHA256={hashes['REVIEW_BINDING_SHA256']}
DESTRUCTIVE_STORAGE_APPLY=APPROVED
REAL_TRADING_ENABLED=NO
POSTGRESQL_FILESYSTEM_DELETION=NO
POLYMARKET_MUTATION=NO

If any condition fails, emit SECOND_PASS_GATE=FAIL and do not emit DESTRUCTIVE_STORAGE_APPLY=APPROVED.
"""

    timers = [
        "hyperliquid-ai-team-orchestrator.timer",
        "hyperliquid-ai-external-supervisor.timer",
    ]
    services = [
        "hyperliquid-ai-team-orchestrator.service",
        "hyperliquid-ai-external-supervisor.service",
    ]
    for name in timers + services:
        stop_unit(name)

    try:
        orch.claude_runtime_preflight()
        hidden = Path(f"/tmp/p0-90-hidden-{task_id}")
        hidden.mkdir(parents=True, exist_ok=True)
        hidden.chmod(0o000)
        home = orch.CLAUDE_HOME
        shell = f"""set -euo pipefail
mount --make-rprivate /
mount --bind {hidden} /mnt
mount --bind {hidden} /root
exec setpriv --reuid={uid} --regid={gid} --init-groups --no-new-privs env -i \\
  HOME={home} CLAUDE_CONFIG_DIR={home / '.claude'} XDG_CONFIG_HOME={home / '.config'} XDG_CACHE_HOME={home / '.cache'} \\
  PATH=/usr/local/bin:/usr/bin:/bin \\
  /usr/bin/claude -p --model opus --max-turns 30 --output-format json \\
  --permission-mode dontAsk --allowedTools Read,Glob,Grep,Bash
"""
        o.runtime.run_started(run_id, task, prompt=prompt, systemd_unit="DIRECT_RESTRICTED_OPUS")
        cp = subprocess.run(
            ["unshare", "--mount", "--fork", "bash", "-c", shell],
            input=prompt,
            text=True,
            capture_output=True,
            cwd=str(workdir),
            timeout=900,
        )
        combined = cp.stdout + ("\n" + cp.stderr if cp.stderr else "")
        log_path.write_text(orch.bounded_redacted(combined))
        session_id, usage, result = orch.parse_claude_output(cp.stdout)
        ledger.close_run(
            run_id,
            exit_code=cp.returncode,
            session_id=session_id,
            usage=usage,
            result=result,
            error=None if cp.returncode == 0 else combined[-1500:],
        )
        if cp.returncode != 0:
            ledger.update(
                task_id,
                status="STALE",
                last_error=f"direct Claude failed rc={cp.returncode}",
                session_id=session_id,
                systemd_unit=None,
            )
            o.finish_runtime_run(
                run_id,
                task_id,
                stdout=cp.stdout,
                stderr=cp.stderr,
                exit_code=cp.returncode,
                session_id=session_id,
                usage=usage,
                result=result,
                error=f"direct Claude failed rc={cp.returncode}",
                status="STALE",
            )
            raise RuntimeError(f"Claude rc={cp.returncode}: {combined[-1500:]}")

        expected = {
            "SECOND_PASS_GATE": "PASS",
            "MODEL_CLASS": "OPUS",
            "ASSIGNMENT_ID": task_id,
            "TARGET_SHA": CODE_SHA,
            "PLAN_RUN_ID": PLAN_RUN_ID,
            **hashes,
            "DESTRUCTIVE_STORAGE_APPLY": "APPROVED",
            "REAL_TRADING_ENABLED": "NO",
            "POSTGRESQL_FILESYSTEM_DELETION": "NO",
            "POLYMARKET_MUTATION": "NO",
        }
        for key, value in expected.items():
            values = re.findall(rf"(?mi)^\s*{re.escape(key)}\s*=\s*([^\s]+)\s*$", result or "")
            if values != [value]:
                ledger.update(
                    task_id,
                    status="STALE",
                    last_error=f"missing/wrong marker {key}",
                    session_id=session_id,
                    systemd_unit=None,
                )
                o.finish_runtime_run(
                    run_id,
                    task_id,
                    stdout=cp.stdout,
                    stderr=cp.stderr,
                    exit_code=1,
                    session_id=session_id,
                    usage=usage,
                    result=result,
                    error=f"missing/wrong marker {key}",
                    status="STALE",
                )
                raise RuntimeError(f"missing/wrong marker {key}: {values}")

        ledger.update(
            task_id,
            status="DONE",
            last_error=None,
            session_id=session_id,
            retry_at=None,
            systemd_unit=None,
        )
        o.finish_runtime_run(
            run_id,
            task_id,
            stdout=cp.stdout,
            stderr=cp.stderr,
            exit_code=0,
            session_id=session_id,
            usage=usage,
            result=result,
            status="PASS",
            blockers=[],
        )

        verifier = [
            sys.executable,
            str(RUNTIME_SCRIPTS / "verify_trusted_opus_storage_review.py"),
            "--db",
            str(DB),
            "--runtime-root",
            str(RUNTIME_ROOT),
            "--assignment-id",
            task_id,
            "--code-sha",
            CODE_SHA,
            "--plan-run-id",
            PLAN_RUN_ID,
            "--plan-completed-at",
            completed,
            "--retention-sha",
            hashes["RETENTION_MANIFEST_SHA256"],
            "--bootstrap-sha",
            hashes["BOOTSTRAP_PLAN_SHA256"],
            "--lifecycle-sha",
            hashes["LIFECYCLE_MANIFEST_SHA256"],
            "--binding-sha",
            hashes["REVIEW_BINDING_SHA256"],
        ]
        verify = subprocess.run(verifier, text=True, capture_output=True, timeout=60)
        if verify.returncode != 0:
            raise RuntimeError(f"trusted verifier rejected Opus result: {verify.stdout[-1000:]} {verify.stderr[-1000:]}")

        print("TRUSTED_OPUS_VERIFIER=PASS")
        print(f"OPUS_ASSIGNMENT_ID={task_id}")
        print(f"PLAN_RUN_ID={PLAN_RUN_ID}")
        print(f"PLAN_COMPLETED_AT={completed}")
        print(f"CODE_SHA={CODE_SHA}")
        for key, value in hashes.items():
            print(f"{key}={value}")
        print("REAL_TRADING_ENABLED=NO")
        print("POSTGRESQL_FILESYSTEM_DELETION=NO")
        print("POLYMARKET_MUTATION=NO")
        return 0
    finally:
        for name in timers:
            start_unit(name)


if __name__ == "__main__":
    raise SystemExit(main())
