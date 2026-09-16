from pathlib import Path


def test_lane3_deploy_defers_reset_until_v3_selector_is_present() -> None:
    deploy = Path("scripts/deploy_invo_notification_executor_self_hosted.sh").read_text()

    assert "EVIDENCE_EPOCH=lane3-hybrid-v3-clean-20260916" in deploy
    assert "EXPECTED_SELECTOR=invo-portfolio-hybrid-v3-20260916" in deploy
    assert "reset_deferred=1" in deploy
    assert "selector_v3_not_deployed" in deploy
    assert "reset_lane3_shadow_epoch.py" in deploy
    assert "portfolio-candidate-cli.js" in deploy
    assert "refusing Lane 3 selector rollback" in deploy


def test_portfolio_research_deploy_uses_source_selector_version() -> None:
    deploy = Path("scripts/deploy_invo_portfolio_research_self_hosted.sh").read_text()

    assert "ELITE_SELECTOR_VERSION" in deploy
    assert "selectorMatch" in deploy
    assert "candidate.selectorVersion !== expectedSelector" in deploy
    assert "invo-portfolio-elite-v1-20260916" not in deploy
