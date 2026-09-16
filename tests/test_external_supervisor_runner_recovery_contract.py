from pathlib import Path


WORKFLOW = Path(".github/workflows/install-external-autonomy-supervisor-timer.yml")


def test_host_supervisor_recovers_only_existing_inactive_runner_services() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "recover_actions_runner()" in text
    assert "actions.runner.*.service" in text
    assert 'systemctl is-active "$unit"' in text
    assert 'systemctl restart "$unit"' in text
    assert "RUNNER_RESTART_COOLDOWN_SEC=600" in text
    assert "runner_restart_cooldown" in text
    assert "runner_recovered" in text
    assert "runner_recovery_failed" in text

    # The host timer may restart an already-installed service, but runner
    # registration/token mutation remains explicitly outside this recovery path.
    assert "config.sh --url" not in text
    assert "registration-token" not in text
    assert "remove-token" not in text


def test_runner_recovery_precedes_orchestrator_preflight() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    recovery_call = text.index("          recover_actions_runner\n")
    orchestrator_preflight = text.index('          if [ ! -x "$ORCH" ]; then')
    assert recovery_call < orchestrator_preflight
