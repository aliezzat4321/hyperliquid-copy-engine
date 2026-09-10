from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
REPAIR = REPO / ".github/workflows/p0-lane-runtime-repair.yml"
ACCEPT = REPO / ".github/workflows/p0-shadow-collection-acceptance.yml"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_long_running_lane_services_never_block_repair_runner() -> None:
    text = _text(REPAIR)
    long_running = (
        "hyperliquid-wallet-research.service",
        "hyperliquid-selective-shadow.service",
        "hyperliquid-external-coverage.service",
        "hyperliquid-external-resolver.service",
        "hyperliquid-invo-wallet-identifier.service",
        "hyperliquid-invo-verified-shadow-sync.service",
    )
    for unit in long_running:
        assert f"systemctl start {unit}" not in text
        assert f"systemctl start --no-block {unit}" in text
    assert "FINAL_RECORD_PROOF=REQUIRES_ISSUE_267" in text
    assert "REAL_TRADING_ENABLED=YES" not in text


def test_shadow_acceptance_dispatches_long_producers_nonblocking() -> None:
    text = _text(ACCEPT)
    long_running = (
        "hyperliquid-selective-shadow.service",
        "hyperliquid-invo-wallet-identifier.service",
        "hyperliquid-invo-verified-shadow-sync.service",
    )
    for unit in long_running:
        assert f"systemctl start {unit}" not in text
        assert f"systemctl start --no-block {unit}" in text
    assert "FINAL_RECORD_PROOF=REQUIRES_ISSUE_267" in text
    assert "REAL_TRADING_ENABLED=YES" not in text
