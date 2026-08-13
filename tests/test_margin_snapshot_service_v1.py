from pathlib import Path


def test_margin_snapshot_service_forces_real_trading_off() -> None:
    text = Path("deploy/systemd/hlcopy-margin-snapshot.service").read_text(encoding="utf-8")
    assert "Environment=REAL_TRADING_ENABLED=NO" in text
    assert "margin_snapshot_cli" in text
