from pathlib import Path


def test_self_hosted_deploy_workflow_is_main_only_and_not_pr_triggered() -> None:
    workflow = Path(".github/workflows/deploy-wallet-identifier.yml").read_text(
        encoding="utf-8"
    )
    assert "runs-on: self-hosted" in workflow
    assert "pull_request:" not in workflow
    assert "- main" in workflow
    assert "persist-credentials: false" in workflow
    assert "cancel-in-progress: false" in workflow


def test_self_hosted_deploy_script_is_fail_closed() -> None:
    script = Path("scripts/deploy_wallet_identifier_self_hosted.sh").read_text(
        encoding="utf-8"
    )
    assert "git merge --ff-only origin/main" in script
    assert "git reset --hard" not in script
    assert "git diff --quiet" in script
    assert "git diff --cached --quiet" in script
    assert "mountpoint -q" in script
    assert "systemd-analyze verify" in script
    assert "hyperliquid-invo-wallet-identifier.service" in script
    assert "hyperliquid-invo-verified-shadow-sync.service" in script
    assert "WALLET_IDENTIFIER_TARGET_STATUS" in script
