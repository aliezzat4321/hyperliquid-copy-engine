import asyncio
import time
from pathlib import Path

from hlcopy.shadow.registry import WalletRegistry, WalletSpec
from hlcopy.shadow.wide_enrich_live import ProspectiveWideTradeOfficialEnricher
from hlcopy.shadow.wide_live import ProspectiveWideTradeCollector


class _MemorySink:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    async def put(self, row: dict[str, object]) -> None:
        self.rows.append(row)


class _NoCallClient:
    async def user_fills_by_time(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("REST must not be called for skipped evidence")


def _address(digit: str) -> str:
    return "0x" + digit * 40


def test_wide_live_guard_drops_prestart_and_stale_rows(tmp_path: Path) -> None:
    registry = WalletRegistry(tmp_path / "wallets.json")
    registry.init()
    tracked_address = _address("a")
    registry.add(
        WalletSpec(
            id="tracked",
            label="tracked",
            source_type="hyperliquid_wallet",
            source_ref=tracked_address,
            stage="research",
        )
    )
    coins_file = tmp_path / "coins.txt"
    coins_file.write_text("BTC\n", encoding="utf-8")
    sink = _MemorySink()
    collector = ProspectiveWideTradeCollector(
        ws_url="wss://example.invalid/ws",
        registry=registry,
        coins_file=coins_file,
        sink=sink,  # type: ignore[arg-type]
        max_live_lag_ms=2_000,
    )
    tracked = {tracked_address: registry.load()[0]}

    prestart_ms = collector.started_ms - 1
    asyncio.run(
        collector._record_trade(
            trade={
                "coin": "BTC",
                "side": "B",
                "px": "64000",
                "sz": "0.1",
                "hash": "0xabc",
                "time": prestart_ms,
                "tid": 1,
                "users": [tracked_address, _address("b")],
            },
            tracked=tracked,
            received_at_ns=(prestart_ms + 100) * 1_000_000,
            received_monotonic_ns=1,
        )
    )
    assert not sink.rows

    live_ms = max(collector.started_ms, int(time.time() * 1000))
    asyncio.run(
        collector._record_trade(
            trade={
                "coin": "BTC",
                "side": "B",
                "px": "64000",
                "sz": "0.1",
                "hash": "0xdef",
                "time": live_ms,
                "tid": 2,
                "users": [tracked_address, _address("b")],
            },
            tracked=tracked,
            received_at_ns=(live_ms + 2_500) * 1_000_000,
            received_monotonic_ns=2,
        )
    )
    assert not sink.rows

    asyncio.run(
        collector._record_trade(
            trade={
                "coin": "BTC",
                "side": "B",
                "px": "64000",
                "sz": "0.1",
                "hash": "0xghi",
                "time": live_ms,
                "tid": 3,
                "users": [tracked_address, _address("b")],
            },
            tracked=tracked,
            received_at_ns=(live_ms + 250) * 1_000_000,
            received_monotonic_ns=3,
        )
    )
    assert len(sink.rows) == 1


def test_enricher_skips_prestart_without_rest(tmp_path: Path) -> None:
    sink = _MemorySink()
    enricher = ProspectiveWideTradeOfficialEnricher(
        source_dir=tmp_path,
        checkpoint_path=tmp_path / "checkpoint.json",
        client=_NoCallClient(),
        sink=sink,  # type: ignore[arg-type]
    )
    event = {
        "kind": "public_wallet_trade",
        "wallet_address": _address("a"),
        "coin": "BTC",
        "tid": 1,
        "exchange_ts_ms": 1,
        "received_at_ns": enricher.started_ns - 1,
    }
    row = asyncio.run(enricher.enrich(event))
    assert row["reason"] == "PRESTART_BACKFILL"
    assert enricher.skipped_prestart == 1
