from pathlib import Path


def test_margin_snapshot_timer_is_periodic() -> None:
    text = Path("deploy/systemd/hlcopy-margin-snapshot.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=30min" in text
    assert "Persistent=true" in text
