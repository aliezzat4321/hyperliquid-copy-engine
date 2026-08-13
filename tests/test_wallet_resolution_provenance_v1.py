from decimal import Decimal

from hlcopy.resolver.provenance import (
    EvidenceSnapshot,
    jsonable_config,
    sha256_bytes,
    sha256_file,
)
from hlcopy.resolver.public_trade_index import PublicTradeDiscoveryConfig
from hlcopy.signals.generic_csv import load_generic_closed_trades_bytes


def test_sha256_changes_when_evidence_changes(tmp_path) -> None:
    evidence = tmp_path / "trades.csv"
    evidence.write_text("a,b\n1,2\n", encoding="utf-8")
    first = sha256_file(evidence)
    evidence.write_text("a,b\n1,3\n", encoding="utf-8")
    second = sha256_file(evidence)
    assert first != second
    assert len(first) == 64
    assert len(second) == 64


def test_evidence_snapshot_binds_parsing_hash_and_size_to_same_bytes(tmp_path) -> None:
    evidence = tmp_path / "trades.csv"
    original = (
        b"symbol,position_side,avg_entry_price,avg_exit_price,start_time,end_time\n"
        b"BTC,LONG,100,110,2026-08-01T10:00:00Z,2026-08-01T11:00:00Z\n"
    )
    evidence.write_bytes(original)
    snapshot = EvidenceSnapshot.from_path(evidence)

    evidence.write_bytes(original.replace(b"110", b"999"))
    imported = load_generic_closed_trades_bytes(snapshot.data)

    assert snapshot.data == original
    assert snapshot.size == len(original)
    assert snapshot.sha256 == sha256_bytes(original)
    assert imported.signals[0].exit_price == Decimal("110")
    assert snapshot.sha256 != sha256_file(evidence)


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
