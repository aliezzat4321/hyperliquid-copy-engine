from decimal import Decimal

from hlcopy.resolver.provenance import jsonable_config, sha256_file
from hlcopy.resolver.public_trade_index import PublicTradeDiscoveryConfig


def test_sha256_changes_when_evidence_changes(tmp_path) -> None:
    evidence = tmp_path / "trades.csv"
    evidence.write_text("a,b\n1,2\n", encoding="utf-8")
    first = sha256_file(evidence)
    evidence.write_text("a,b\n1,3\n", encoding="utf-8")
    second = sha256_file(evidence)
    assert first != second
    assert len(first) == 64
    assert len(second) == 64


def test_effective_config_serializes_all_gate_values() -> None:
    config = PublicTradeDiscoveryConfig(
        max_price_bps=Decimal("12.5"),
        historical_entry_time_tolerance_ms=123_456,
        historical_entry_price_tolerance_bps=Decimal("7.5"),
        min_historical_winner_match_gap=4,
    )
    payload = jsonable_config(config)
    assert set(payload) == set(config.__dataclass_fields__)
    assert payload["max_price_bps"] == "12.5"
    assert payload["historical_entry_time_tolerance_ms"] == 123_456
    assert payload["historical_entry_price_tolerance_bps"] == "7.5"
    assert payload["min_historical_winner_match_gap"] == 4
