import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from hlcopy.shadow.wide_enrich import WideTradeOfficialEnricher, _match_fill


@dataclass
class _Page:
    response_payload: object


class _FakeClient:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, int, int | None]] = []

    async def user_fills_by_time(
        self,
        user: str,
        start_time_ms: int,
        end_time_ms: int | None = None,
    ) -> list[_Page]:
        self.calls.append((user, start_time_ms, end_time_ms))
        return [_Page(self.rows)]


class _MemorySink:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    async def put(self, row: dict[str, object]) -> None:
        self.rows.append(row)


def _event() -> dict[str, object]:
    return {
        "kind": "public_wallet_trade",
        "wallet_id": "hl-a",
        "wallet_label": "a",
        "wallet_stage": "research",
        "wallet_address": "0x" + "a" * 40,
        "coin": "kBONK",
        "target_side": "BUY",
        "exchange_ts_ms": 1_780_000_000_000,
        "received_at_ns": 1_780_000_000_250_000_000,
        "received_monotonic_ns": 99,
        "observed_event_lag_ms": 250.0,
        "tid": 123,
        "px": "0.001",
        "sz": "1000",
    }


def _official_fill() -> dict[str, object]:
    return {
        "coin": "kBONK",
        "px": "0.001",
        "sz": "1000",
        "side": "B",
        "time": 1_780_000_000_000,
        "startPosition": "0",
        "dir": "Open Long",
        "closedPnl": "0",
        "hash": "0xabc",
        "oid": 456,
        "crossed": True,
        "fee": "0.45",
        "feeToken": "USDC",
        "tid": 123,
    }


def test_match_fill_preserves_case_significant_native_symbol() -> None:
    assert _match_fill(
        [_official_fill()],
        tid=123,
        coin="kBONK",
        exchange_ts_ms=1_780_000_000_000,
    ) == _official_fill()
    assert (
        _match_fill(
            [_official_fill()],
            tid=123,
            coin="KBONK",
            exchange_ts_ms=1_780_000_000_000,
        )
        is None
    )


def test_enrichment_recovers_authoritative_position_action(tmp_path: Path) -> None:
    client = _FakeClient([_official_fill()])
    sink = _MemorySink()
    enricher = WideTradeOfficialEnricher(
        source_dir=tmp_path / "source",
        checkpoint_path=tmp_path / "checkpoint.json",
        client=client,
        sink=sink,  # type: ignore[arg-type]
        retry_delays=(0.0,),
    )

    row = asyncio.run(enricher.enrich(_event()))

    assert row["kind"] == "wide_official_fill"
    assert row["official_dir"] == "Open Long"
    assert row["official_start_position"] == "0"
    assert row["official_oid"] == 456
    assert row["official_crossed"] is True
    assert row["public_observed_lag_ms"] == 250.0
    assert len(client.calls) == 1
    assert sink.rows == [row]


def test_drain_checkpoints_and_does_not_requery_processed_event(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    path = source / "2026-08-11.jsonl"
    path.write_text(json.dumps(_event()) + "\n", encoding="utf-8")

    client = _FakeClient([_official_fill()])
    sink = _MemorySink()
    checkpoint = tmp_path / "state" / "checkpoint.json"
    enricher = WideTradeOfficialEnricher(
        source_dir=source,
        checkpoint_path=checkpoint,
        client=client,
        sink=sink,  # type: ignore[arg-type]
        retry_delays=(0.0,),
    )

    assert asyncio.run(enricher.drain_once()) == 1
    assert asyncio.run(enricher.drain_once()) == 0
    assert len(client.calls) == 1
    assert checkpoint.exists()


def test_enrichment_miss_is_explicit(tmp_path: Path) -> None:
    client = _FakeClient([])
    sink = _MemorySink()
    enricher = WideTradeOfficialEnricher(
        source_dir=tmp_path / "source",
        checkpoint_path=tmp_path / "checkpoint.json",
        client=client,
        sink=sink,  # type: ignore[arg-type]
        retry_delays=(0.0,),
    )

    row = asyncio.run(enricher.enrich(_event()))

    assert row["kind"] == "wide_official_fill_miss"
    assert row["reason"] == "OFFICIAL_FILL_NOT_FOUND"
    assert row["attempts"] == 1


def test_wide_fill_enrichment_service_is_shadow_only() -> None:
    unit = Path("deploy/systemd/hyperliquid-wide-fill-enrichment.service").read_text()
    assert "hlcopy.shadow.wide_enrich_cli" in unit
    assert "shadow/wide-trades" in unit
    assert "shadow/wide-enriched" in unit
    assert "REAL_TRADING_ENABLED=NO" in unit
