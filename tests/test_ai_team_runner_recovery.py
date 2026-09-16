import importlib.util
import subprocess
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ai_team_runner_recovery.py"
spec = importlib.util.spec_from_file_location("ai_team_runner_recovery", MODULE_PATH)
assert spec and spec.loader
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def completed(cmd: list[str], rc: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, rc, stdout, "")


def test_discovery_accepts_only_enabled_intended_runner(monkeypatch):
    intended = (
        "actions.runner.aliezzat4321-hyperliquid-copy-engine."
        "signal-engine-hyperliquid.service"
    )

    def fake_run(cmd, *, timeout=20):
        del timeout
        assert cmd[:2] == ["systemctl", "list-unit-files"]
        return completed(
            cmd,
            stdout=(
                f"{intended} enabled enabled\n"
                "actions.runner.signal-engine-hyperliquid-old.service disabled enabled\n"
                "actions.runner.signal-engine-hyperliquid-static.service static enabled\n"
                "actions.runner.other-enabled.service enabled enabled\n"
                "ssh.service enabled enabled\n"
            ),
        )

    monkeypatch.setattr(recovery, "run", fake_run)
    assert recovery.discover_runner_units() == [intended]


def test_active_runner_is_never_restarted(monkeypatch):
    calls: list[list[str]] = []
    unit = "actions.runner.repo.signal-engine-hyperliquid.service"

    def fake_run(cmd, *, timeout=20):
        del timeout
        calls.append(cmd)
        if cmd[1] == "is-active":
            return completed(cmd, stdout="active\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(recovery, "run", fake_run)
    row = recovery.recover_unit(unit, {}, recovery.utcnow())
    assert row["action"] == "none"
    assert row["after"] == "active"
    assert not any("restart" in cmd for cmd in calls)


def test_inactive_intended_runner_gets_bounded_restart(monkeypatch):
    calls: list[list[str]] = []
    active_checks = 0
    unit = "actions.runner.repo.signal-engine-hyperliquid.service"

    def fake_run(cmd, *, timeout=20):
        nonlocal active_checks
        del timeout
        calls.append(cmd)
        if cmd[1] == "is-active":
            active_checks += 1
            return completed(cmd, stdout="inactive\n" if active_checks == 1 else "active\n")
        if cmd[1] in {"reset-failed", "restart"}:
            return completed(cmd)
        raise AssertionError(cmd)

    monkeypatch.setattr(recovery, "run", fake_run)
    state = {}
    row = recovery.recover_unit(unit, state, recovery.utcnow())
    assert row["action"] == "restart"
    assert row["recovered"] is True
    assert ["systemctl", "reset-failed", unit] in calls
    assert ["systemctl", "restart", unit] in calls
    assert state["attempts"][unit]["epoch"] > 0


def test_unrelated_runner_is_out_of_scope_even_if_directly_passed(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, *, timeout=20):
        del timeout
        calls.append(cmd)
        raise AssertionError(cmd)

    monkeypatch.setattr(recovery, "run", fake_run)
    row = recovery.recover_unit(
        "actions.runner.repo.other-enabled.service", {}, recovery.utcnow()
    )
    assert row["action"] == "out_of_scope"
    assert calls == []


def test_disabled_or_unrelated_runner_is_never_restarted(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    active_checks = 0
    intended = "actions.runner.repo.signal-engine-hyperliquid.service"
    disabled = "actions.runner.repo.signal-engine-hyperliquid-old.service"
    unrelated = "actions.runner.repo.other-enabled.service"

    def fake_run(cmd, *, timeout=20):
        nonlocal active_checks
        del timeout
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "list-unit-files"]:
            return completed(
                cmd,
                stdout=(
                    f"{disabled} disabled enabled\n"
                    f"{unrelated} enabled enabled\n"
                    f"{intended} enabled enabled\n"
                ),
            )
        if cmd[1] == "is-active":
            assert cmd[2] == intended
            active_checks += 1
            return completed(cmd, stdout="inactive\n" if active_checks == 1 else "active\n")
        if cmd[1] in {"reset-failed", "restart"}:
            assert cmd[2] == intended
            return completed(cmd)
        raise AssertionError(cmd)

    monkeypatch.setattr(recovery, "run", fake_run)
    monkeypatch.setattr(recovery.os, "geteuid", lambda: 0)
    monkeypatch.setattr(recovery, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(recovery, "STATE_FILE", tmp_path / "runner-recovery.json")

    assert recovery.main() == 0
    assert ["systemctl", "restart", intended] in calls
    assert not any(disabled in cmd for cmd in calls if cmd[:2] != ["systemctl", "list-unit-files"])
    assert not any(unrelated in cmd for cmd in calls if cmd[:2] != ["systemctl", "list-unit-files"])


def test_restart_cooldown_prevents_restart_storm(monkeypatch):
    calls: list[list[str]] = []
    now = recovery.utcnow()
    unit = "actions.runner.repo.signal-engine-hyperliquid.service"
    state = {
        "attempts": {
            unit: {
                "epoch": int(now.timestamp()),
                "at": recovery.iso(now),
            }
        }
    }

    def fake_run(cmd, *, timeout=20):
        del timeout
        calls.append(cmd)
        if cmd[1] == "is-active":
            return completed(cmd, stdout="failed\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(recovery, "run", fake_run)
    row = recovery.recover_unit(unit, state, now)
    assert row["action"] == "cooldown"
    assert not any("restart" in cmd for cmd in calls)


def test_runner_recovery_has_no_registration_or_token_mutation_path():
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "config.sh" not in text
    assert "registration-token" not in text
    assert "remove-token" not in text
    assert "REAL_TRADING_CHANGE=NO" in text
    assert "POLYMARKET_TOUCHED=NO" in text
