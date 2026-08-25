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
