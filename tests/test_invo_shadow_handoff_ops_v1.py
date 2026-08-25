from __future__ import annotations

import subprocess
from pathlib import Path


def test_identifier_triggers_verified_shadow_sync_and_bootstrap_installs_it() -> None:
    identifier = Path(
        "deploy/systemd/hyperliquid-invo-wallet-identifier.service"
    ).read_text(encoding="utf-8")
    sync = Path(
        "deploy/systemd/hyperliquid-invo-verified-shadow-sync.service"
    ).read_text(encoding="utf-8")
    bootstrap = Path("scripts/bootstrap_invo_source_miner.sh").read_text(encoding="utf-8")

    assert "OnSuccess=hyperliquid-invo-verified-shadow-sync.service" in identifier
    assert "REAL_TRADING_ENABLED=NO" in sync
    assert "verified_identity_shadow_sync" in sync
    assert "/var/lib/hyperliquid-copy-engine/invo/identified_wallets.json" in sync
    assert "/mnt/HC_Volume_106576526/hyperliquid/shadow/wallets.json" in sync
    assert "ReadWritePaths=/mnt/HC_Volume_106576526/hyperliquid/shadow" in sync
    assert "SYNC_SERVICE_NAME" in bootstrap
    assert 'install_rendered_unit "${SYNC_SERVICE_NAME}"' in bootstrap
    assert 'systemctl start "${SYNC_SERVICE_NAME}"' in bootstrap


def test_non_default_repo_path_renders_all_invo_pipeline_units(tmp_path: Path) -> None:
    default_repo = "/root/hyperliquid-copy-engine"
    custom_repo = "/opt/wallet-identifier/hyperliquid-copy-engine"
    rendered_paths: list[Path] = []
    for unit_name in (
        "hyperliquid-invo-source-miner.service",
        "hyperliquid-invo-wallet-identifier.service",
        "hyperliquid-invo-verified-shadow-sync.service",
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
