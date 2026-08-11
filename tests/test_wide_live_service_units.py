from pathlib import Path


def test_live_wide_service_units_are_isolated_and_fail_closed() -> None:
    watch = Path("deploy/systemd/hyperliquid-wide-trade-watch-live.service").read_text()
    enrich = Path("deploy/systemd/hyperliquid-wide-fill-enrichment-live.service").read_text()

    assert "wide_cli_live" in watch
    assert "wide-trades-live" in watch
    assert "--max-live-lag-ms 2000" in watch
    assert "REAL_TRADING_ENABLED=NO" in watch

    assert "wide_enrich_cli_live" in enrich
    assert "wide-trades-live" in enrich
    assert "wide-enriched-live" in enrich
    assert "--max-event-age-ms 10000" in enrich
    assert "REAL_TRADING_ENABLED=NO" in enrich
