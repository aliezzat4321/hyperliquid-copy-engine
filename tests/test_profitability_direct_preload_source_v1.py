from pathlib import Path


def test_position_live_cli_preloads_direct_events_once() -> None:
    source = Path("src/hlcopy/profitability/position_live_cli.py").read_text(encoding="utf-8")
    assert "direct_by_wallet =" in source
    assert "events = direct_by_wallet[wallet.id]" in source
