from pathlib import Path


def test_lane3_deploy_defers_reset_until_v4_selector_is_present() -> None:
    deploy = Path("scripts/deploy_invo_notification_executor_self_hosted.sh").read_text()

    assert "EVIDENCE_EPOCH=lane3-hybrid-v4-clean-20260921" in deploy
    assert "EXPECTED_SELECTOR=invo-portfolio-hybrid-v4-20260921" in deploy
    assert "reset_deferred=1" in deploy
    assert "selector_v4_not_deployed" in deploy
    assert "reset_lane3_shadow_epoch.py" in deploy
    assert "portfolio-candidate-cli.js" in deploy
    assert "refusing Lane 3 selector rollback" in deploy


def test_lane3_clean_epoch_seed_loads_runtime_environment() -> None:
    deploy = Path("scripts/deploy_invo_notification_executor_self_hosted.sh").read_text()

    seed = deploy.index('node dist/src/portfolio-candidate-cli.js')
    state_root = "/var/lib/hyperliquid-copy-engine/invo-notification-executor"
    expected_paths = {
        "INVO_PORTFOLIO_CANDIDATE_STATE_PATH": "portfolio-candidates.json",
        "INVO_PORTFOLIO_CANDIDATE_SNAPSHOTS_PATH": "portfolio-candidate-snapshots.jsonl",
        "INVO_PORTFOLIO_LEADERBOARD_STATE_PATH": "invo-leaderboards.json",
        "INVO_PORTFOLIO_LEADERBOARD_SNAPSHOTS_PATH": "invo-leaderboard-snapshots.jsonl",
    }
    for key, filename in expected_paths.items():
        assert f"set_env {key} {state_root}/{filename}" in deploy
    assert deploy.index('source "$INVO_ENV"') < seed
    assert deploy.index('source "$EXEC_ENV"') < seed
    assert deploy.index("set -a", deploy.index('python3 "$RESET_SCRIPT"')) < seed
    assert deploy.index("set +a", deploy.index('python3 "$RESET_SCRIPT"')) < seed


def test_portfolio_research_deploy_uses_source_selector_version() -> None:
    deploy = Path("scripts/deploy_invo_portfolio_research_self_hosted.sh").read_text()

    assert "ELITE_SELECTOR_VERSION" in deploy
    assert "selectorMatch" in deploy
    assert "candidate.selectorVersion !== expectedSelector" in deploy
    assert "invo-portfolio-elite-v1-20260916" not in deploy


def test_lane3_deploy_pins_durable_funding_boundary_path() -> None:
    deploy = Path("scripts/deploy_invo_notification_executor_self_hosted.sh").read_text()
    expected = (
        "set_env NOTIFICATION_TRADER_FUNDING_BOUNDARY_PATH "
        "/var/lib/hyperliquid-copy-engine/invo-notification-executor/funding-boundaries"
    )
    assert expected in deploy


def test_lane3_deploy_validates_integrated_health_before_resume_or_epoch_marker() -> None:
    deploy = Path("scripts/deploy_invo_notification_executor_self_hosted.sh").read_text()
    validation = deploy.index("validate_lane3_shadow_health.py")
    assert validation < deploy.index('systemctl start "$RESEARCH_SERVICE"')
    assert validation < deploy.index('> "$EVIDENCE_MARKER"')
    assert "clean v3" not in deploy


def test_lane3_deploy_retries_transient_integrated_health_but_is_bounded() -> None:
    deploy = Path("scripts/deploy_invo_notification_executor_self_hosted.sh").read_text()
    assert "health_deadline=$((SECONDS + 90))" in deploy
    assert "while (( SECONDS < health_deadline ))" in deploy
    assert 'health_valid=1' in deploy
    assert '[[ "$health_valid" -ne 1 ]]' in deploy
