import subprocess
from pathlib import Path


def test_source_miner_triggers_identifier_and_bootstrap_installs_it() -> None:
    source = Path("deploy/systemd/hyperliquid-invo-source-miner.service").read_text(
        encoding="utf-8"
    )
    identifier = Path(
        "deploy/systemd/hyperliquid-invo-wallet-identifier.service"
    ).read_text(encoding="utf-8")
    bootstrap = Path("scripts/bootstrap_invo_source_miner.sh").read_text(encoding="utf-8")

    assert "OnSuccess=hyperliquid-invo-wallet-identifier.service" in source
    assert "REAL_TRADING_ENABLED=NO" in identifier
    assert "--priority-trader carmine --priority-trader bones" in identifier
    lock_path = "/run/hyperliquid-copy-engine/invo-pipeline.lock"
    assert lock_path in source
    assert lock_path in identifier
    assert "RuntimeDirectory=hyperliquid-copy-engine" in source
    assert "RuntimeDirectory=hyperliquid-copy-engine" in identifier
    assert "IDENTIFIER_SERVICE_NAME" in bootstrap
    assert "rollback_environment" in bootstrap
    assert 'install_rendered_unit "${SERVICE_NAME}"' in bootstrap
    assert 'install_rendered_unit "${IDENTIFIER_SERVICE_NAME}"' in bootstrap
    assert 's|/root/hyperliquid-copy-engine|${REPO_DIR}|g' in bootstrap


def test_non_default_repo_path_renders_valid_installed_units(tmp_path: Path) -> None:
    default_repo = "/root/hyperliquid-copy-engine"
    custom_repo = "/opt/wallet-identifier/hyperliquid-copy-engine"
    rendered_paths: list[Path] = []
    for unit_name in (
        "hyperliquid-invo-source-miner.service",
        "hyperliquid-invo-wallet-identifier.service",
    ):
        template = Path("deploy/systemd", unit_name).read_text(encoding="utf-8")
        rendered = template.replace(default_repo, custom_repo)
        assert default_repo not in rendered
        assert f"WorkingDirectory={custom_repo}" in rendered
        assert f"{custom_repo}/.venv/bin/python" in rendered
        output = tmp_path / unit_name
        output.write_text(rendered, encoding="utf-8")
        rendered_paths.append(output)

    subprocess.run(
        ["systemd-analyze", "verify", *(str(path) for path in rendered_paths)],
        check=True,
        capture_output=True,
        text=True,
    )
